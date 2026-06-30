"""
Trelvy Memory System (Prisma-schema-aligned)
==============================================
Matches the actual schema: a single `AgentMemory` table discriminated by
`AgentMemoryType` (WORKING/EPISODIC/SEMANTIC/RETRIEVAL/PROCEDURAL/CACHE/
COLLABORATION) and a single `ModelMemory` table discriminated by
`ModelMemoryType` (CONTEXT/KNOWLEDGE/RETRIEVAL/PERSISTENT/CACHE/CONVERSATION),
both keyed off `SubAgent.id` (Int).

Embeddings are NOT stored in Postgres (no vector column in this schema) -
they live in Qdrant, and `AgentMemory.embeddingId` / `ModelMemory.embeddingId`
just store the Qdrant point id as a string pointer.

Ephemeral types (WORKING, CACHE, COLLABORATION) use `expiresAt` for TTL
instead of Redis - Postgres is the single source of truth here.
"""

import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient, models as qmodels

from database import db
from schema import AgentMemoryType, ModelMemoryType  # generated Prisma enums

openai_client = AsyncOpenAI()
qdrant = AsyncQdrantClient(url="http://localhost:6333")

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
COLLECTION = "trelvy_memory"

GOLDFISH_TTL = timedelta(minutes=30)
SQUIRREL_TTL = timedelta(hours=6)
DOLPHIN_TTL = timedelta(hours=2)


async def ensure_collection():
    collections = await qdrant.get_collections()
    if COLLECTION not in [c.name for c in collections.collections]:
        await qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=qmodels.VectorParams(size=EMBEDDING_DIM, distance=qmodels.Distance.COSINE),
        )


async def _embed(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


async def _resolve_subagent_id(agent_unique_id: str) -> int:
    """All memory functions take the public uniqueId; resolve to the int FK."""
    sub_agent = await db.subagent.find_first(where={"uniqueId": agent_unique_id})
    if not sub_agent:
        raise ValueError(f"No SubAgent found with uniqueId={agent_unique_id}")
    return sub_agent.id


# ---------------------------------------------------------------------------
# Generic AgentMemory read/write (shared by all 7 memory types)
# ---------------------------------------------------------------------------

async def _write_agent_memory(
    sub_agent_id: int, mem_type: AgentMemoryType, content: dict,
    title: Optional[str] = None, importance: float = 0.5, confidence: float = 1.0,
    tags: Optional[list[str]] = None, embedding_id: Optional[str] = None,
    ttl: Optional[timedelta] = None,
) -> Any:
    return await db.agentmemory.create(
        data={
            "subAgentId": sub_agent_id,
            "type": mem_type,
            "title": title,
            "content": json.dumps(content) if not isinstance(content, str) else content,
            "importance": importance,
            "confidence": confidence,
            "tags": tags or [],
            "embeddingId": embedding_id,
            "expiresAt": (datetime.utcnow() + ttl) if ttl else None,
        }
    )


async def _read_agent_memory(
    sub_agent_id: int, mem_type: AgentMemoryType, limit: int = 20,
    memory_id: Optional[int] = None, only_active: bool = True,
) -> Any:
    if memory_id:
        return await db.agentmemory.find_first(where={"id": memory_id})

    where: dict = {"subAgentId": sub_agent_id, "type": mem_type}
    if only_active:
        # active = no expiry, or expiry in the future
        where["OR"] = [{"expiresAt": None}, {"expiresAt": {"gt": datetime.utcnow()}}]

    return await db.agentmemory.find_many(
        where=where, order={"createdAt": "desc"}, take=limit,
    )


async def _clear_agent_memory(sub_agent_id: int, mem_type: AgentMemoryType, memory_id: Optional[int] = None):
    if memory_id:
        await db.agentmemory.delete(where={"id": memory_id})
        return True
    await db.agentmemory.delete_many(where={"subAgentId": sub_agent_id, "type": mem_type})
    return True


# ---------------------------------------------------------------------------
# 🐟 GOLDFISH - WORKING MEMORY (Postgres, TTL via expiresAt)
# ---------------------------------------------------------------------------

async def goldfish(action: str, agent_id: str, key: Optional[str] = None,
                    value: Optional[dict] = None) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if action == "write":
        return await _write_agent_memory(
            sub_agent_id, AgentMemoryType.WORKING, content=value or {},
            title=key, importance=(value or {}).get("importance", 0.3), ttl=GOLDFISH_TTL,
        )
    if action == "read":
        return await _read_agent_memory(sub_agent_id, AgentMemoryType.WORKING, limit=50)
    if action == "clear":
        return await _clear_agent_memory(sub_agent_id, AgentMemoryType.WORKING)
    raise ValueError(f"Unknown goldfish action: {action}")


# ---------------------------------------------------------------------------
# 🐘 ELEPHANT - EPISODIC MEMORY (Postgres, permanent)
# ---------------------------------------------------------------------------

async def elephant(action: str, agent_id: str, event: Optional[dict] = None,
                    memory_id: Optional[int] = None, limit: int = 20) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if action == "write":
        return await _write_agent_memory(
            sub_agent_id, AgentMemoryType.EPISODIC, content=event or {},
            title=(event or {}).get("summary", "")[:120],
            importance=(event or {}).get("importance", 0.5),
            tags=[(event or {}).get("outcome", "unknown")],
        )
    if action == "read":
        return await _read_agent_memory(sub_agent_id, AgentMemoryType.EPISODIC, limit, memory_id)
    raise ValueError(f"Unknown elephant action: {action}")


# ---------------------------------------------------------------------------
# 🦉 OWL - SEMANTIC MEMORY (Postgres, permanent)
# ---------------------------------------------------------------------------

async def owl(action: str, agent_id: str, fact: Optional[dict] = None,
               topic: Optional[str] = None, limit: int = 20) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if action == "write":
        return await _write_agent_memory(
            sub_agent_id, AgentMemoryType.SEMANTIC, content=fact or {},
            title=(fact or {}).get("topic", "general"),
            confidence=(fact or {}).get("confidence", 0.7),
            tags=[(fact or {}).get("topic", "general")],
        )
    if action == "read":
        results = await _read_agent_memory(sub_agent_id, AgentMemoryType.SEMANTIC, limit)
        if topic:
            results = [r for r in results if r.title == topic or topic in (r.tags or [])]
        return results
    raise ValueError(f"Unknown owl action: {action}")


# ---------------------------------------------------------------------------
# 🐕 BLOODHOUND - RETRIEVAL MEMORY (Postgres row + Qdrant vector)
# ---------------------------------------------------------------------------

async def bloodhound(action: str, agent_id: str, text: Optional[str] = None,
                      metadata: Optional[dict] = None, top_k: int = 5) -> Any:
    await ensure_collection()
    sub_agent_id = await _resolve_subagent_id(agent_id)

    if action == "write":
        vector = await _embed(text)
        point_id = str(uuid.uuid4())
        await qdrant.upsert(
            collection_name=COLLECTION,
            points=[qmodels.PointStruct(
                id=point_id, vector=vector,
                payload={"agent_id": agent_id, "sub_agent_id": sub_agent_id,
                         "text": text, **(metadata or {})},
            )],
        )
        return await _write_agent_memory(
            sub_agent_id, AgentMemoryType.RETRIEVAL, content={"text": text, **(metadata or {})},
            title=text[:120], embedding_id=point_id,
        )

    if action == "search":
        vector = await _embed(text)
        hits = await qdrant.search(
            collection_name=COLLECTION, query_vector=vector, limit=top_k,
            query_filter=qmodels.Filter(
                must=[qmodels.FieldCondition(key="sub_agent_id", match=qmodels.MatchValue(value=sub_agent_id))]
            ),
        )
        return [{"score": h.score, "text": h.payload.get("text"), "metadata": h.payload} for h in hits]

    raise ValueError(f"Unknown bloodhound action: {action}")


# ---------------------------------------------------------------------------
# 🦫 BEAVER - PROCEDURAL MEMORY (Postgres, permanent)
# ---------------------------------------------------------------------------

async def beaver(action: str, agent_id: str, procedure: Optional[dict] = None,
                  name: Optional[str] = None, limit: int = 20) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if action == "write":
        return await _write_agent_memory(
            sub_agent_id, AgentMemoryType.PROCEDURAL, content=procedure or {},
            title=(procedure or {}).get("name"),
            confidence=(procedure or {}).get("success_rate", 1.0),
        )
    if action == "read":
        results = await _read_agent_memory(sub_agent_id, AgentMemoryType.PROCEDURAL, limit)
        if name:
            results = [r for r in results if r.title == name]
        return results
    raise ValueError(f"Unknown beaver action: {action}")


# ---------------------------------------------------------------------------
# 🐿️ SQUIRREL - CACHE MEMORY (Postgres, TTL via expiresAt)
# ---------------------------------------------------------------------------

async def squirrel(action: str, agent_id: str, key: str,
                    value: Optional[dict] = None, ttl: timedelta = SQUIRREL_TTL) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if action == "write":
        # overwrite any existing entry with the same key
        await db.agentmemory.delete_many(
            where={"subAgentId": sub_agent_id, "type": AgentMemoryType.CACHE, "title": key}
        )
        return await _write_agent_memory(
            sub_agent_id, AgentMemoryType.CACHE, content=value or {}, title=key, ttl=ttl,
        )
    if action == "read":
        rows = await _read_agent_memory(sub_agent_id, AgentMemoryType.CACHE, limit=1)
        match = next((r for r in rows if r.title == key), None)
        if not match:
            match = await db.agentmemory.find_first(
                where={"subAgentId": sub_agent_id, "type": AgentMemoryType.CACHE, "title": key},
                order={"createdAt": "desc"},
            )
        return json.loads(match.content) if match else None
    if action == "clear":
        await db.agentmemory.delete_many(
            where={"subAgentId": sub_agent_id, "type": AgentMemoryType.CACHE, "title": key}
        )
        return True
    raise ValueError(f"Unknown squirrel action: {action}")


# ---------------------------------------------------------------------------
# 🐬 DOLPHIN - COLLABORATION MEMORY (Postgres, shared, TTL via expiresAt)
# ---------------------------------------------------------------------------

async def dolphin(action: str, channel: str, agent_id: Optional[str] = None,
                   payload: Optional[dict] = None, limit: int = 50) -> Any:
    if action == "write":
        sub_agent_id = await _resolve_subagent_id(agent_id)
        return await _write_agent_memory(
            sub_agent_id, AgentMemoryType.COLLABORATION, content=payload or {},
            title=channel, tags=[channel], ttl=DOLPHIN_TTL,
        )
    if action == "read":
        rows = await db.agentmemory.find_many(
            where={
                "type": AgentMemoryType.COLLABORATION, "title": channel,
                "OR": [{"expiresAt": None}, {"expiresAt": {"gt": datetime.utcnow()}}],
            },
            order={"createdAt": "asc"}, take=limit,
        )
        return rows
    raise ValueError(f"Unknown dolphin action: {action}")


# ---------------------------------------------------------------------------
# MODEL MEMORY (ModelMemory table, discriminated by ModelMemoryType)
# ---------------------------------------------------------------------------

async def _write_model_memory(sub_agent_id: int, mem_type: ModelMemoryType,
                               prompt: Any, response: Optional[Any] = None,
                               tokens: Optional[int] = None, summary: Optional[str] = None,
                               embedding_id: Optional[str] = None) -> Any:
    return await db.modelmemory.create(
        data={
            "subAgentId": sub_agent_id,
            "type": mem_type,
            "prompt": json.dumps(prompt) if not isinstance(prompt, str) else prompt,
            "response": (json.dumps(response) if response is not None and not isinstance(response, str) else response),
            "tokens": tokens,
            "summary": summary,
            "embeddingId": embedding_id,
        }
    )


async def context(agent_id: str, value: Optional[str] = None) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if value is not None:
        return await _write_model_memory(sub_agent_id, ModelMemoryType.CONTEXT, prompt=value)
    rows = await db.modelmemory.find_many(
        where={"subAgentId": sub_agent_id, "type": ModelMemoryType.CONTEXT},
        order={"createdAt": "desc"}, take=1,
    )
    return rows[0].prompt if rows else None


async def conversation(agent_id: str, message: Optional[dict] = None, limit: int = 30) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if message is not None:
        return await _write_model_memory(
            sub_agent_id, ModelMemoryType.CONVERSATION,
            prompt=message.get("content", ""), response=None,
            summary=message.get("role"),
        )
    rows = await db.modelmemory.find_many(
        where={"subAgentId": sub_agent_id, "type": ModelMemoryType.CONVERSATION},
        order={"createdAt": "asc"}, take=limit,
    )
    return [{"role": r.summary, "content": r.prompt} for r in rows]


async def knowledge(agent_id: str, value: Optional[dict] = None) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if value is not None:
        return await _write_model_memory(
            sub_agent_id, ModelMemoryType.KNOWLEDGE, prompt=value, summary="extracted_knowledge",
        )
    rows = await db.modelmemory.find_many(
        where={"subAgentId": sub_agent_id, "type": ModelMemoryType.KNOWLEDGE},
        order={"createdAt": "desc"}, take=10,
    )
    return rows


async def retrieval(agent_id: str, text: str, top_k: int = 5) -> Any:
    """Relevant retrieved context for the model - sourced from Bloodhound's Qdrant search."""
    results = await bloodhound("search", agent_id, text=text, top_k=top_k)
    sub_agent_id = await _resolve_subagent_id(agent_id)
    await _write_model_memory(
        sub_agent_id, ModelMemoryType.RETRIEVAL, prompt=text,
        response=results, summary=f"{len(results)} matches",
    )
    return results


async def cache(agent_id: str, key: str, value: Optional[Any] = None) -> Any:
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if value is not None:
        return await _write_model_memory(
            sub_agent_id, ModelMemoryType.CACHE, prompt=key, response=value,
        )
    row = await db.modelmemory.find_first(
        where={"subAgentId": sub_agent_id, "type": ModelMemoryType.CACHE, "prompt": key},
        order={"createdAt": "desc"},
    )
    return json.loads(row.response) if row and row.response else None


async def persistent(agent_id: str, key: str, value: Optional[Any] = None) -> Any:
    """Long-term model-specific information. Always the latest row for a given key wins."""
    sub_agent_id = await _resolve_subagent_id(agent_id)
    if value is not None:
        return await _write_model_memory(
            sub_agent_id, ModelMemoryType.PERSISTENT, prompt=key, response=value,
        )
    row = await db.modelmemory.find_first(
        where={"subAgentId": sub_agent_id, "type": ModelMemoryType.PERSISTENT, "prompt": key},
        order={"createdAt": "desc"},
    )
    return json.loads(row.response) if row and row.response else None


# ---------------------------------------------------------------------------
# MEMORY CONSOLIDATION
# ---------------------------------------------------------------------------

async def consolidate(agent_id: str):
    """
    Promote important Goldfish (working) entries up the memory chain:
    Goldfish -> Elephant -> extract knowledge -> Owl -> extract procedure
    -> Beaver -> generate embedding -> Bloodhound.
    Only promotes entries with importance >= 0.5; clears promoted Goldfish rows.
    """
    sub_agent_id = await _resolve_subagent_id(agent_id)
    entries = await _read_agent_memory(sub_agent_id, AgentMemoryType.WORKING, limit=100)
    promoted = []

    for entry in entries:
        value = json.loads(entry.content) if isinstance(entry.content, str) else entry.content
        if (entry.importance or 0) < 0.5:
            continue

        episodic = await elephant("write", agent_id, event={
            "summary": value.get("summary", str(value)[:200]),
            "outcome": value.get("outcome", "unknown"),
            "details": value,
            "importance": entry.importance,
            "task_id": value.get("task_id"),
        })

        if value.get("fact"):
            await owl("write", agent_id, fact={
                "topic": value.get("topic", "general"),
                "fact": value["fact"],
                "confidence": value.get("confidence", 0.7),
            })

        if value.get("procedure"):
            await beaver("write", agent_id, procedure=value["procedure"])

        searchable_text = value.get("summary") or value.get("fact") or str(value)
        await bloodhound("write", agent_id, text=searchable_text, metadata={
            "episodic_id": episodic.id, "importance": entry.importance,
        })

        await db.agentmemory.delete(where={"id": entry.id})
        promoted.append(entry.id)

    return {"promoted_count": len(promoted)}