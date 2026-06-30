"""
Trelvy Sandbox Router (Docker-based)
=======================================
Manages isolated Docker containers that agents are spun up into for
execution (the "Agent pulled from GitHub -> spun up in sandbox ->
connected peer-to-peer" layer of the Clause architecture).

Replaces E2B with plain Docker - no usage billing, runs entirely on your
own machine/server via the local Docker daemon. Each sandbox is a real
container tied to a swarm agent (coordination.AgentState) and a
multi-agent record.

Requires Docker Desktop (or the Docker engine) running locally, and the
`docker` Python package: `uv add docker`
"""

import io
import json
import tarfile
import time
import uuid

import docker
from docker.errors import NotFound, APIError
from fastapi import APIRouter, HTTPException, Depends
from starlette import status

from database import db
from schema import SandboxDetails, SandboxRunRequest
from runtime.auth import get_current_user

import coordination as coord
import memory as mem

router = APIRouter(
    prefix="/sandboxes",
    tags=["Sandboxes"],
)

docker_client = docker.from_env()

DEFAULT_IMAGE = "python:3.12-slim"
CONTAINER_IDLE_TIMEOUT = 60 * 15  # 15 min, enforced by a reaper job (see bottom)
CPU_LIMIT = 1.0       # 1 vCPU
MEMORY_LIMIT = "512m"


def _container_name(sandbox_id: str) -> str:
    return f"trelvy-sandbox-{sandbox_id}"


# ---------------------------------------------------------------------------
# Create + boot a sandbox for an agent
# ---------------------------------------------------------------------------

@router.post("/", response_model=SandboxDetails, status_code=status.HTTP_201_CREATED)
async def create_sandbox(
    details: SandboxDetails,
    current_user=Depends(get_current_user),
):
    agent_record = await db.multiAgent.find_first(
        where={"uniqueId": details.agentId, "userId": current_user.id}
    )
    if not agent_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent_record.swarmAgentId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent must be registered with the Coordination Engine before sandboxing",
        )

    sandbox_id = str(uuid.uuid4())
    image = details.template or DEFAULT_IMAGE

    try:
        docker_client.images.get(image)
    except NotFound:
        docker_client.images.pull(image)

    container = docker_client.containers.run(
        image=image,
        name=_container_name(sandbox_id),
        command="tail -f /dev/null",  # keep the container alive between exec calls
        detach=True,
        labels={
            "trelvy_sandbox_id": sandbox_id,
            "agent_id": agent_record.swarmAgentId,
            "user_id": str(current_user.id),
        },
        nano_cpus=int(CPU_LIMIT * 1_000_000_000),
        mem_limit=MEMORY_LIMIT,
        network_mode="bridge",
        # no host filesystem mounts -> fully isolated from the host
    )

    if details.repoUrl:
        exit_code, output = container.exec_run(f"git clone {details.repoUrl} /workspace")
        if exit_code != 0:
            container.kill()
            container.remove()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to clone repo: {output.decode(errors='ignore')}",
            )
        if details.setupCommand:
            container.exec_run(details.setupCommand, workdir="/workspace")

    record = await db.sandbox.create(data={
        "uniqueId": sandbox_id,
        "agentId": agent_record.uniqueId,
        "swarmAgentId": agent_record.swarmAgentId,
        "dockerContainerId": container.id,
        "template": image,
        "repoUrl": details.repoUrl,
        "status": "running",
        "userId": current_user.id,
    })

    await coord.update_agent_state(agent_record.swarmAgentId, status="sandboxed")
    await coord.publish_event(coord.EventType.STATUS, {
        "agent_id": agent_record.swarmAgentId,
        "sandbox_id": sandbox_id,
        "message": "Sandbox container booted",
    })
    await mem.squirrel("write", agent_record.swarmAgentId, key="active_sandbox", value={
        "sandbox_id": sandbox_id, "container_id": container.id, "booted_at": time.time(),
    })

    return record


# ---------------------------------------------------------------------------
# Run code/commands inside a live sandbox container
# ---------------------------------------------------------------------------

@router.post("/{unique_id}/run", status_code=status.HTTP_200_OK)
async def run_in_sandbox(
    unique_id: str,
    run_request: SandboxRunRequest,
    current_user=Depends(get_current_user),
):
    record = await db.sandbox.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id}
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox not found")

    try:
        container = docker_client.containers.get(record.dockerContainerId)
    except NotFound:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Sandbox container no longer exists")

    if container.status != "running":
        container.start()

    if run_request.language == "shell":
        cmd = run_request.code
    else:
        # write the snippet to a temp file inside the container, then run it,
        # avoids quoting/escaping issues with passing code directly as a -c arg
        ext = {"python": "py", "node": "js"}.get(run_request.language, "txt")
        runner = {"python": "python3", "node": "node"}.get(run_request.language, "cat")
        filename = f"/tmp/snippet_{uuid.uuid4().hex}.{ext}"
        _write_file_to_container(container, filename, run_request.code)
        cmd = f"{runner} {filename}"

    exit_code, output = container.exec_run(
        cmd, workdir=run_request.cwd or "/workspace", demux=True,
    )
    stdout, stderr = output
    result = {
        "exit_code": exit_code,
        "stdout": (stdout or b"").decode(errors="ignore"),
        "stderr": (stderr or b"").decode(errors="ignore"),
    }

    if record.swarmAgentId:
        await mem.goldfish("write", record.swarmAgentId, key=f"sandbox_run:{int(time.time())}", value={
            "summary": f"Ran sandbox command: {run_request.code[:120]}",
            "outcome": "success" if exit_code == 0 else "failure",
            "importance": "low",
        })

    await db.sandbox.update(where={"uniqueId": unique_id}, data={"lastRunAt": time.time()})
    return result


def _write_file_to_container(container, path: str, content: str):
    """Stream a file into a running container via tarball (docker-py's put_archive API)."""
    data = content.encode()
    tarstream = io.BytesIO()
    with tarfile.open(fileobj=tarstream, mode="w") as tar:
        info = tarfile.TarInfo(name=path.lstrip("/"))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tarstream.seek(0)
    container.put_archive("/", tarstream)


# ---------------------------------------------------------------------------
# Get sandbox details
# ---------------------------------------------------------------------------

@router.get("/{unique_id}", response_model=SandboxDetails, status_code=status.HTTP_200_OK)
async def get_sandbox(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    record = await db.sandbox.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id}
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox not found")
    return record


# ---------------------------------------------------------------------------
# List all sandboxes for the logged in user (optionally filter by agent)
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[SandboxDetails], status_code=status.HTTP_200_OK)
async def list_sandboxes(
    agent_id: str | None = None,
    current_user=Depends(get_current_user),
):
    where = {"userId": current_user.id}
    if agent_id:
        where["agentId"] = agent_id
    return await db.sandbox.find_many(where=where, order={"createdAt": "desc"})


# ---------------------------------------------------------------------------
# Kill / delete a sandbox container
# ---------------------------------------------------------------------------

@router.delete("/{unique_id}", status_code=status.HTTP_200_OK)
async def delete_sandbox(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    record = await db.sandbox.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id}
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox not found")

    try:
        container = docker_client.containers.get(record.dockerContainerId)
        container.kill()
        container.remove()
    except NotFound:
        pass  # already gone
    except APIError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    await db.sandbox.update(where={"uniqueId": unique_id}, data={"status": "terminated"})

    if record.swarmAgentId:
        await coord.update_agent_state(record.swarmAgentId, status="idle")
        await mem.squirrel("clear", record.swarmAgentId, key="active_sandbox")

    return {"detail": "Sandbox terminated"}


# ---------------------------------------------------------------------------
# Reaper: kills containers idle past CONTAINER_IDLE_TIMEOUT.
# Run this periodically (e.g. via an APScheduler job or a cron-triggered
# endpoint) since Docker itself won't clean these up on its own.
# ---------------------------------------------------------------------------

async def reap_idle_sandboxes():
    cutoff = time.time() - CONTAINER_IDLE_TIMEOUT
    stale = await db.sandbox.find_many(
        where={"status": "running", "lastRunAt": {"lt": cutoff}}
    )
    for record in stale:
        try:
            container = docker_client.containers.get(record.dockerContainerId)
            container.kill()
            container.remove()
        except NotFound:
            pass
        await db.sandbox.update(where={"uniqueId": record.uniqueId}, data={"status": "terminated"})
        if record.swarmAgentId:
            await coord.update_agent_state(record.swarmAgentId, status="idle")
    return {"reaped": len(stale)}