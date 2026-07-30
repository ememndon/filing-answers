# Two stages, so the image that ships does not carry a compiler.
#
# The wheels are built once in the first stage and copied into the
# second, which keeps the shipped layer to a Python runtime and the
# libraries themselves. It is smaller, and more to the point there is
# less in it: a build toolchain in a production image is a set of tools
# available to anyone who gets a shell in it.

FROM python:3.12-slim AS build

WORKDIR /build

# Copied before the source, so a change to a source file does not
# invalidate the layer that installed the dependencies.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.12-slim

# Unbuffered so logs appear as they happen rather than when a buffer
# fills, which in a container is the difference between watching a
# problem and finding it afterwards.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl is here for the health check below and nothing else.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

WORKDIR /app

# The evaluation set is data the gate reads at runtime, so it travels
# with the image rather than being fetched. It goes beside the working
# directory rather than inside the package, because it is meant to be
# read and argued with — and the gate looks for it here.
COPY evaluation ./evaluation

# Not root. A process that only reads public filings and calls one API
# has no need of it, and the day something goes wrong is the wrong day to
# discover that it had it.
RUN useradd --create-home --uid 10001 answers \
 && mkdir -p /app/data \
 && chown -R answers:answers /app
USER answers

# No .env is copied and none should be. Configuration arrives as
# environment variables, which is what keeps a key out of an image that
# gets pushed to a registry and shared.
EXPOSE 8000

# Readiness rather than liveness: this asks whether the process can do
# its job, which is the question an orchestrator wants answered before
# sending it traffic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/ready || exit 1

CMD ["uvicorn", "filing_answers.api:app", "--host", "0.0.0.0", "--port", "8000"]
