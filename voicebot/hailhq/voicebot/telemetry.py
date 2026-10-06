"""Connect native LiveKit spans without replacing a Cloud exporter."""

from hailhq.core.telemetry import share_native_provider
from livekit.agents.telemetry import set_tracer_provider, tracer
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider


def connect_livekit_tracing() -> None:
    # Agents 1.6 exposes its dynamic tracer publicly but not a provider getter.
    # Verified against pinned telemetry/traces.py. Preserve a Cloud SDK provider
    # if the job runner installed one before invoking Hail's entrypoint.
    native = getattr(tracer, "_tracer_provider", None)
    if isinstance(native, TracerProvider):
        share_native_provider(native)
    else:
        # https://docs.livekit.io/testing/observability/tracing/
        set_tracer_provider(trace.get_tracer_provider())
