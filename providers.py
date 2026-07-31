"""Three annexes, one door.

The clerk's thinking is rented, and each server rents it from whoever it
likes: Google's Gemini, xAI's Grok, or Anthropic's Claude. A house may
hold keys to all three and switch between them without losing a word of
anything else.

brain.py knows nothing about either wire format. It builds a neutral
transcript, hands it to whichever provider the server chose, and gets
back a `Reply`. Everything provider-shaped lives in here, so a third
annex would be one more class and no edits anywhere else.

Fidelity note: a model turn carries a `raw` field holding the provider's
own representation of what it said. Replaying that verbatim is what keeps
multi-step tool calls working: reasoning models sign their turns, and a
rebuilt-from-neutral copy loses the signature.

Prices below are dollars per million tokens, and they are estimates for
each provider's cheap fast model. They exist to keep the monthly budget
honest, not to be an invoice. An annex that has named its models one by
one (`model_prices`) is billed at whichever one answered; anywhere else,
a server on a costlier model should set price_in_per_m and
price_out_per_m for itself.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("providers")

TIMEOUT = aiohttp.ClientTimeout(total=90)


class ProviderError(Exception):
    """Carries `.code` so the retry logic in brain.py can tell a busy
    annex (retry) from a refused key (do not)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


@dataclass
class Call:
    name: str
    args: dict
    id: str = ""


@dataclass
class Reply:
    text: str = ""
    calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0   # served from cache, billed at a fraction
    cache_write: int = 0  # written to cache, billed at a small premium
    raw: object = None  # the provider's own copy of this turn, replayed verbatim

    # tokens_in always means *uncached* input, whatever the annex reports.
    # Gemini and Grok count cached tokens inside their prompt total and
    # Anthropic counts them alongside it; each class below subtracts where
    # it must, so the caller can price the three buckets without counting
    # any token twice.


# ---------- the system prompt, in two halves ----------

def _system_parts(system):
    """A system prompt as a list of pieces, the first of which is the part
    worth caching.

    Eugene's character and the standing orders are identical on every
    single request and account for most of what he is told; the memory
    book and today's date change under him. Caching is a prefix match, so the
    stable half has to come first and be sent byte-identical every time --
    one interpolated timestamp near the top and nothing after it can be
    reused. Callers signal the split by handing over a list; a plain
    string is treated as wholly volatile and never cached, because a cache
    write costs more than a plain read and would never be repaid.
    """
    if not system:
        return []
    if isinstance(system, str):
        return [system]
    return [part for part in system if part]


def joined_system(system):
    """The whole prompt as one string, for annexes that cache on their own."""
    return "\n".join(_system_parts(system))


# ---------- the neutral transcript ----------

def said(text):
    return {"role": "user", "text": text}


def answered(reply: Reply):
    return {"role": "model", "text": reply.text, "calls": reply.calls, "raw": reply.raw}


def returned(results):
    """results: [{"id", "name", "result"}]"""
    return {"role": "tool", "results": results}


# ---------- Gemini ----------

class Gemini:
    name = "gemini"
    label = "Gemini"
    key_hint = "Google AI Studio → Get API key"
    default_model = os.environ.get("CLERK_GEMINI_MODEL", "gemini-3.1-flash-lite")
    # The one for the long look. Never used for chat: a clerk who answers
    # in a sentence does not need it, and the bill for having it answer
    # every "morning" is not one anybody agreed to.
    deep_model = os.environ.get("CLERK_GEMINI_DEEP", "gemini-3.1-pro")
    price_in = 0.25
    price_out = 1.50
    # Gemini caches long repeated prefixes by itself, with nothing to ask
    # for and nothing to pay to write one. All that is left to do is count
    # the discount honestly.
    cache_read_rate = 0.25
    cache_write_rate = 1.00

    def __init__(self, api_key):
        from google import genai

        self._client = genai.Client(api_key=api_key)

    def _contents(self, turns):
        from google.genai import types

        out = []
        for turn in turns:
            if turn["role"] == "user":
                out.append(
                    types.Content(role="user", parts=[types.Part(text=turn["text"])])
                )
            elif turn["role"] == "model":
                if turn.get("raw") is not None:
                    out.append(turn["raw"])
                    continue
                parts = [types.Part(text=turn.get("text") or "")]
                out.append(types.Content(role="model", parts=parts))
            else:
                out.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=r["name"], response={"result": r["result"]}
                            )
                            for r in turn["results"]
                        ],
                    )
                )
        return out

    async def converse(self, model, system, turns, tools=None, max_tokens=400,
                       temperature=0.7):
        from google.genai import types

        options = dict(
            system_instruction=joined_system(system),
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
        if tools:
            options["tools"] = [types.Tool(function_declarations=tools)]
        config = types.GenerateContentConfig(**options)
        response = await self._client.aio.models.generate_content(
            model=model, contents=self._contents(turns), config=config
        )
        usage = response.usage_metadata
        # The prompt count here includes whatever came out of the cache, so
        # the cached share is taken back out to leave the tokens actually
        # charged at full rate.
        prompt = getattr(usage, "prompt_token_count", 0) or 0
        cached = getattr(usage, "cached_content_token_count", 0) or 0
        reply = Reply(
            tokens_in=max(prompt - cached, 0),
            tokens_out=getattr(usage, "candidates_token_count", 0) or 0,
            cache_read=cached,
        )
        candidate = response.candidates[0] if response.candidates else None
        if candidate is None or candidate.content is None:
            return reply
        reply.raw = candidate.content
        reply.text = (response.text or "").strip()
        reply.calls = [
            Call(name=p.function_call.name, args=dict(p.function_call.args or {}))
            for p in (candidate.content.parts or [])
            if getattr(p, "function_call", None)
        ]
        return reply

    async def json_answer(self, model, prompt, schema, max_tokens=300):
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        usage = response.usage_metadata
        return (
            _loads(response.text),
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
        )

    async def list_models(self):
        try:
            names = []
            async for model in await self._client.aio.models.list():
                if model.name:
                    names.append(model.name.removeprefix("models/"))
            return sorted(names)
        except Exception as e:
            log.warning(f"could not list gemini models: {e!r}")
            return []


# ---------- Grok (xAI, OpenAI-compatible) ----------

class Grok:
    name = "grok"
    label = "Grok"
    key_hint = "console.x.ai → API keys"
    base = os.environ.get("CLERK_GROK_BASE", "https://api.x.ai/v1").rstrip("/")
    default_model = os.environ.get("CLERK_GROK_MODEL", "grok-4-fast")
    deep_model = os.environ.get("CLERK_GROK_DEEP", "grok-4")
    price_in = 0.20
    price_out = 0.50
    # Like Gemini, cached automatically and for free; only the discount on
    # a hit needs recording.
    cache_read_rate = 0.25
    cache_write_rate = 1.00

    def __init__(self, api_key):
        self._key = api_key

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path, payload):
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(
                f"{self.base}{path}", headers=self._headers(), json=payload
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise ProviderError(
                        _terse(body), code=response.status
                    )
                return json.loads(body)

    def _messages(self, system, turns):
        system = joined_system(system)
        messages = [{"role": "system", "content": system}] if system else []
        for turn in turns:
            if turn["role"] == "user":
                messages.append({"role": "user", "content": turn["text"]})
            elif turn["role"] == "model":
                if turn.get("raw") is not None:
                    messages.append(turn["raw"])
                    continue
                messages.append(
                    {"role": "assistant", "content": turn.get("text") or ""}
                )
            else:
                for r in turn["results"]:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": r["id"],
                            "content": r["result"],
                        }
                    )
        return messages

    async def converse(self, model, system, turns, tools=None, max_tokens=400,
                       temperature=0.7):
        payload = {
            "model": model,
            "messages": self._messages(system, turns),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]
        data = await self._post("/chat/completions", payload)
        usage = data.get("usage") or {}
        # As with Gemini, the prompt total already counts the cached tokens.
        prompt = usage.get("prompt_tokens", 0) or 0
        cached = ((usage.get("prompt_tokens_details") or {})
                  .get("cached_tokens", 0) or 0)
        reply = Reply(
            tokens_in=max(prompt - cached, 0),
            tokens_out=usage.get("completion_tokens", 0) or 0,
            cache_read=cached,
        )
        choices = data.get("choices") or []
        if not choices:
            return reply
        message = choices[0].get("message") or {}
        reply.raw = message
        reply.text = (message.get("content") or "").strip()
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            reply.calls.append(
                Call(
                    name=fn.get("name", ""),
                    args=_loads(fn.get("arguments")),
                    id=call.get("id", ""),
                )
            )
        return reply

    async def json_answer(self, model, prompt, schema, max_tokens=300):
        # json_object rather than a strict schema: every OpenAI-compatible
        # host supports it, and the one caller treats a miss as harmless.
        data = await self._post(
            "/chat/completions",
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": f"{prompt}\n\nAnswer with JSON matching "
                                   f"this schema:\n{json.dumps(schema)}",
                    }
                ],
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
        )
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        text = (choices[0].get("message", {}).get("content") if choices else "") or ""
        return (
            _loads(text),
            usage.get("prompt_tokens", 0) or 0,
            usage.get("completion_tokens", 0) or 0,
        )

    async def list_models(self):
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.get(
                    f"{self.base}/models", headers=self._headers()
                ) as response:
                    if response.status >= 400:
                        return []
                    data = await response.json()
            return sorted(m["id"] for m in data.get("data") or [] if m.get("id"))
        except Exception as e:
            log.warning(f"could not list grok models: {e!r}")
            return []


# ---------- Claude (Anthropic) ----------

# Claude thinks before it answers unless told not to, and here it is told
# not to. The clerk replies in a sentence or two out of a 400 token
# ceiling that covers thinking and speech together, so a reasoning turn
# would spend the lot on itself and get cut off mid-word.
#
# Two things go wrong with thinking off, both documented and both cheap
# to guard against: a tool call occasionally arrives written out as prose
# instead of made as a call, and internal tags sometimes leak into the
# reply. Hence the two sentences below, which go only to this annex, and
# the strip in `_visible`. Never add "do not think" to either: it makes
# the leaking worse, not better.
NO_THINKING = (
    "\n\nYou may say a brief sentence before using a tool. Do not include "
    "internal or system XML tags in your response."
)

LEAKED_TAGS = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)


def _visible(text):
    return LEAKED_TAGS.sub("", text or "").strip()


def _cached_system_blocks(system):
    """The system prompt as Anthropic content blocks, with the cache mark.

    The annex reads tools first, then the system prompt, then the
    conversation, and caches everything up to the mark. One mark at the end
    of the stable half therefore carries the tool declarations with it for
    free, and leaves what he remembers and today's date -- which move --
    outside it, where they belong.

    A caller who hands over one undivided string gets no mark at all. That
    is deliberate: writing a cache costs more than reading the tokens
    plainly, so caching something that changes every time is worse than not
    caching it, and it would fail silently rather than loudly.

    A prompt below the model's minimum (around a thousand tokens) will not
    cache either -- no error, no hit, and no write premium charged. The
    character and the standing orders clear that several times over.
    """
    parts = _system_parts(system)
    if not parts:
        return []
    parts = list(parts)
    parts[-1] += NO_THINKING
    blocks = [{"type": "text", "text": part} for part in parts]
    if len(blocks) > 1:
        blocks[0]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _closed(schema):
    """A schema, copied, with every object shut.

    The schemas in brain.py are written for Gemini, which does not mind an
    object that leaves room for properties nobody declared. Claude's
    structured outputs refuse one, so close them on the way past rather
    than making the caller keep two copies.
    """
    if isinstance(schema, dict):
        out = {key: _closed(value) for key, value in schema.items()}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [_closed(item) for item in schema]
    return schema


class Claude:
    name = "claude"
    label = "Claude"
    key_hint = "console.anthropic.com → API keys"
    # Haiku, deliberately: the clerk answers in a sentence or two out of a
    # 400 token ceiling, and a frontier model is five times the price for a
    # job this size. A house that wants one types it into the model field in
    # `/setup`, or sets CLERK_CLAUDE_MODEL.
    default_model = os.environ.get("CLERK_CLAUDE_MODEL", "claude-haiku-4-5")
    deep_model = os.environ.get("CLERK_CLAUDE_DEEP", "claude-opus-5")
    # The three rungs, named. Anthropic keeps three tiers in the field at a
    # time, and a house choosing between them is choosing a price rather
    # than a model id, so `/model` takes the name and looks the id up here.
    # Cheapest first: the order is what the command offers.
    tiers = {
        "haiku": "claude-haiku-4-5",
        "sonnet": "claude-sonnet-5",
        "opus": "claude-opus-5",
    }
    # Haiku 4.5 rates, kept as the fallback for a model nobody listed below.
    price_in = 1.00
    price_out = 5.00
    # Dollars per million, per model, so the counter follows the choice
    # instead of billing an opus month at haiku rates. Without this a house
    # that switched tiers would run five times over its budget before the
    # ceiling noticed. Sonnet is priced at its standard rate rather than the
    # introductory one: a budget that errs high stops the clerk early, and a
    # budget that errs low stops it never.
    model_prices = {
        "claude-haiku-4-5": (1.00, 5.00),
        "claude-sonnet-5": (3.00, 15.00),
        "claude-opus-5": (5.00, 25.00),
    }
    # Anthropic is the one annex that caches only when asked, and the one
    # that charges to write a cache: a tenth of the input rate to read a
    # hit, a quarter over it to lay one down. Two requests inside the
    # window pay for the write, and Eugene's standing orders go out on
    # every single one.
    cache_read_rate = 0.10
    cache_write_rate = 1.25

    def __init__(self, api_key):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def _messages(self, turns):
        out = []
        for turn in turns:
            if turn["role"] == "user":
                out.append({"role": "user", "content": turn["text"]})
            elif turn["role"] == "model":
                if turn.get("raw") is not None:
                    out.append({"role": "assistant", "content": turn["raw"]})
                    continue
                out.append(
                    {"role": "assistant", "content": turn.get("text") or ""}
                )
            else:
                # Every result of a round goes back in one user turn:
                # split across several, the API refuses them.
                out.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r["id"],
                            "content": r["result"],
                        }
                        for r in turn["results"]
                    ],
                })
        return out

    async def _send(self, **payload):
        try:
            return await self._client.messages.create(**payload)
        except self._anthropic.APIStatusError as e:
            # The SDK's own message carries the whole body; `_terse` gets
            # the one line of it worth putting in a log.
            body = e.body if isinstance(e.body, dict) else None
            raise ProviderError(
                _terse(json.dumps(body)) if body else str(e)[:200],
                code=e.status_code,
            ) from e
        except self._anthropic.APIError as e:
            raise ProviderError(str(e)[:200]) from e

    async def converse(self, model, system, turns, tools=None, max_tokens=400,
                       temperature=0.7):
        # temperature is taken and dropped: current Claude models refuse
        # sampling parameters outright, and the shape of a reply is set by
        # the system prompt here anyway.
        payload = dict(
            model=model,
            max_tokens=max_tokens,
            messages=self._messages(turns),
            thinking={"type": "disabled"},
        )
        blocks = _cached_system_blocks(system)
        if blocks:
            payload["system"] = blocks
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in tools
            ]
        message = await self._send(**payload)
        usage = message.usage
        # Unlike the other two, this annex reports the cached tokens beside
        # the prompt total rather than inside it, so nothing is taken back
        # out here.
        reply = Reply(
            tokens_in=usage.input_tokens or 0,
            tokens_out=usage.output_tokens or 0,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
        blocks = list(message.content or [])
        reply.raw = blocks or None
        reply.text = _visible(
            "".join(b.text for b in blocks if b.type == "text")
        )
        reply.calls = [
            Call(name=b.name, args=dict(b.input or {}), id=b.id)
            for b in blocks if b.type == "tool_use"
        ]
        if message.stop_reason == "refusal" and not reply.text:
            # A refusal is an answer, not an outage. Say so plainly rather
            # than letting the empty turn read as the annex being down.
            reply.raw = blocks
            reply.text = "I would rather not answer that one."
        return reply

    async def json_answer(self, model, prompt, schema, max_tokens=300):
        message = await self._send(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {"type": "json_schema", "schema": _closed(schema)}
            },
        )
        usage = message.usage
        text = "".join(
            b.text for b in (message.content or []) if b.type == "text"
        )
        return (
            _loads(text),
            usage.input_tokens or 0,
            usage.output_tokens or 0,
        )

    async def list_models(self):
        try:
            names = []
            async for model in self._client.models.list():
                if model.id:
                    names.append(model.id)
            return sorted(names)
        except Exception as e:
            log.warning(f"could not list claude models: {e!r}")
            return []


# ---------- the roster ----------

PROVIDERS = {p.name: p for p in (Gemini, Grok, Claude)}
NAMES = tuple(PROVIDERS)


def label(name):
    provider = PROVIDERS.get(name)
    return provider.label if provider else name


def default_model(name):
    provider = PROVIDERS.get(name)
    return provider.default_model if provider else ""


def deep_model(name):
    """What this annex offers for the one job worth paying for. A server
    that would rather use something else says so; a server that has said
    nothing gets this rather than nothing, because a long look answered by
    the cheap model is the same list with worse judgement on it."""
    provider = PROVIDERS.get(name)
    return getattr(provider, "deep_model", "") if provider else ""


def build(name, api_key):
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(f"no such provider: {name}")
    return provider(api_key)


def prices(name, model=None):
    """Dollars per million in and out, for this annex and -- when it has
    said so -- for this particular model. An annex that names no per-model
    figures, or a model it has never heard of, falls back to the annex's own
    estimate, which is what the counter did before any of this."""
    provider = PROVIDERS.get(name)
    if provider is None:
        return (0.0, 0.0)
    listed = getattr(provider, "model_prices", None) or {}
    return listed.get(model) or (provider.price_in, provider.price_out)


def tiers(name):
    """The named rungs this annex offers, cheapest first, as {name: model}.
    Empty for an annex that has not named any, which is how a caller asks
    whether the choice is worth offering at all."""
    provider = PROVIDERS.get(name)
    return dict(getattr(provider, "tiers", {})) if provider else {}


def tier_of(name, model):
    """Which rung a model id sits on, or None if it is not one of them --
    a house that typed its own model into `/setup` keeps it, and gets told
    the truth rather than the nearest label."""
    for tier, listed in tiers(name).items():
        if listed == model:
            return tier
    return None


def cache_rates(name):
    """(read, write) as multiples of the input rate. A miss on an unknown
    annex is priced as though nothing were cached, which errs high."""
    provider = PROVIDERS.get(name)
    if provider is None:
        return (1.0, 1.0)
    return (provider.cache_read_rate, provider.cache_write_rate)


def _loads(text):
    try:
        return json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _terse(body):
    """An API error body, shortened to something a log line can hold."""
    try:
        data = json.loads(body)
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:200]
        return str(error or data)[:200]
    except (json.JSONDecodeError, AttributeError):
        return (body or "")[:200]
