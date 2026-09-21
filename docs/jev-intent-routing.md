# Jev for Bluesky Research-Turn Intent Routing

## Decision

Use TypeSafe Jev only as an optional, fail-open pre-research intent router. One typed `Choice` selects `SOURCE_BRIEF`, `QUESTION_ANSWER`, or `NOTE_EXPLORE`; OMP remains responsible for retrieval, reasoning, wiki maintenance, and prose.

This boundary follows Jev's design: it returns constrained decisions and probabilities rather than generated text. TypeSafe documents intent routing in front of deterministic code or specialist language models as a target pattern, and states that Jev 1.13 is not a text-generation model ([System One](https://docs.typesafe.ai/concepts/system-one.md), [intent routing](https://docs.typesafe.ai/patterns/intent-routing.md), [model limits](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#generation)).

## Contract

A direct request is `POST https://api.typesafe.ai/v1/systemone` with bearer authentication and a JSON body containing `state`, `model`, and named `questions` ([API reference](https://docs.typesafe.ai/api.md#evaluation-endpoint)). Jev accepts text or structured JSON state. It does not accept image, audio, or video input ([state](https://docs.typesafe.ai/concepts/state.md), [models](https://docs.typesafe.ai/models.md)).

The relevant primitive is `Choice`: it selects one configured option and returns the selected `choice`, every option's `probability`, and a derived `confidence` value ([Choice](https://docs.typesafe.ai/primitives/choice.md), [Choice response](https://docs.typesafe.ai/api.md#choice-answer)).

```json
{
  "type": "choice",
  "choice": "QUESTION_ANSWER",
  "probabilities": {
    "SOURCE_BRIEF": 0.05,
    "QUESTION_ANSWER": 0.90,
    "NOTE_EXPLORE": 0.05
  },
  "confidence": 0.85
}
```

`confidence` describes how concentrated the distribution is; it is not an independent estimate that the selected label is correct. TypeSafe recommends calibrating thresholds on application data rather than treating any threshold as universal ([confidence](https://docs.typesafe.ai/confidence.md)). Calibration is an aggregate property: predictions assigned probability 0.8 should be correct about 80 percent of the time over a suitable population, not on every individual turn ([AI primer](https://docs.typesafe.ai/introduction/machine-learning-primer.md#rlcd-and-calibrated-decisions)).

## Proposed choice

Send only the turn text and small structural facts. Never send branch history or wiki content to the router.

- `SOURCE_BRIEF`: primarily read, summarize, or assess supplied sources without a separate substantive question.
- `QUESTION_ANSWER`: answer a substantive question; a URL plus a question belongs here.
- `NOTE_EXPLORE`: investigate and connect an observation, claim, topic, or branch continuation.

Start with `confidence >= 0.70` as a provisional automatic-routing threshold. Persist the model ID, selected label, probabilities, confidence, and fallback reason so the threshold can be calibrated. Pin a version while calibrating because `jev-latest` can move ([model aliases](https://docs.typesafe.ai/models.md#aliases)).

## Deterministic precedence and fallback

1. A URL plus an explicit question is `QUESTION_ANSWER`.
2. A URL-only turn or a source-reading directive without a separate question is `SOURCE_BRIEF`.
3. A reply without an explicit question or source request inherits its nearest parent's mode.
4. A timeout, transport error, malformed response, missing answer, or confidence below 0.70 falls back to the same structural rules; an otherwise ambiguous root becomes `NOTE_EXPLORE`.
5. Routing failure never blocks research and is never retried solely to obtain a different label.

Jev can read adversarial state literally, performs best in English, and loses accuracy when irrelevant text grows. A selected label must only change presentation and research instructions; it must never expand permissions ([Jev 1.13 limits](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md), [language support](https://docs.typesafe.ai/models.md#language-support)).

## Integration paths

The official Python SDK supports typed question objects, strict response models, configurable timeouts, and synchronous or asynchronous clients ([Python SDK](https://docs.typesafe.ai/sdk/python.md)). Direct HTTP avoids the SDK dependency but requires local schema validation and error handling. TypeSafe documents 401, 422, 429, and 529 responses ([API errors](https://docs.typesafe.ai/api.md#errors)).

Cloudflare serves the model as `typesafe/jev` through its Workers AI run endpoint with the same state/questions shape, separate account credentials, and a 32,000-token provider context limit ([Cloudflare Jev](https://developers.cloudflare.com/ai/models/typesafe/jev/), [Workers AI REST](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)). Cloudflare's OpenAI-compatible documentation does not establish compatibility for Jev's typed state/questions contract.

### Manifest and OpenRouter

The repository uses Manifest's OpenAI-compatible gateway. Manifest documents an OpenAI-compatible URL and model routing through connected providers ([Manifest introduction](https://manifest.build/docs/introduction/), [OpenRouter provider](https://manifest.build/providers/openrouter/)).

**Unknown:** the reviewed first-party TypeSafe, Cloudflare, OpenRouter, and Manifest sources do not establish a typed Jev state/questions wire contract through Manifest. The configured Manifest harness must expose an exact Jev model ID and a response that preserves choice probabilities and confidence before the model-backed route is trusted. Until then, the deterministic router remains authoritative and the model call is optional.

At the time of integration, this Manifest harness exposed 351 model IDs but none containing `jev` or `typesafe`. A structured `auto` request returned Manifest `M101`, identifying the harness as `test` and reporting that no provider was configured. Model-backed routing therefore remains disabled until OpenRouter is attached to that harness and a Jev ID appears.

## Cost and latency

TypeSafe lists Jev 1.13 at $0.042 per million input tokens with output free, a 64,000-token request budget, and dynamically changing service limits ([models](https://docs.typesafe.ai/models.md#current-models)). Its launch post reports 70–500 ms end-to-end measurements, not an SLA ([launch post](https://typesafe.ai/blog/introducing-system-one-models-and-jev#frontiers-old-and-new)). Provider-specific Manifest or OpenRouter cost and latency must be measured from the Microsandbox.

## Recommendation

Implement the three modes and deterministic rules now. Enable the Jev call only after the Manifest harness lists the configured Jev model and a smoke request returns a validated typed distribution. Keep OMP as the only model that researches and writes.
