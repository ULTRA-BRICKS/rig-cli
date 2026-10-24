# Dockerfile — "fresh machine" for the real-agent ticket-gate E2E (rig-cli#756).
#
# One image carries BOTH variants of the code under test, side by side, so a trial picks one at
# RUN time (E2E_VARIANT=base|fixed) and the image is built once per matrix run:
#   /opt/variants/<variant>/agent-tools   the guard catalog   (git archive of a ref)
#   /opt/variants/<variant>/rig-cli       the provisioner     (git archive of a ref)
# Everything stateful (rig apply, omp config, the scratch git repo) happens at run time in
# trial.py, so every `docker run` is a pristine machine.
#
# The build context is staged by run_matrix.py (never built by hand): it holds omp (the host's
# binary, so the agent under test is the one the user runs), the two variant trees, the real
# `task` SKILL.md, and in_container/.
#
# No credentials are baked in: run_matrix.py passes ANTHROPIC_OAUTH_TOKEN at `docker run` time.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/*

# rig's runtime deps (rig-cli/pyproject.toml). One shared venv: both variants run from source.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir pyyaml "textual>=0.66" "rich>=13"
ENV PATH="/opt/venv/bin:${PATH}"

# The agent under test: a compiled binary (libc only), copied from the host so the container
# runs the same omp the user does. `omp` on PATH is the binary itself — no cgroup wrapper.
COPY omp /opt/omp/omp
RUN chmod 0755 /opt/omp/omp && ln -s /opt/omp/omp /usr/local/bin/omp

COPY variants /opt/variants
COPY task-skill /opt/task-skill
COPY in_container /opt/e2e
RUN chmod 0755 /opt/e2e/trial.py /opt/e2e/task_stub.py /opt/e2e/helper_task_unavailable.py

RUN useradd -m -s /bin/bash tester
USER tester
ENV HOME=/home/tester
WORKDIR /home/tester
ENTRYPOINT ["/opt/e2e/trial.py"]
