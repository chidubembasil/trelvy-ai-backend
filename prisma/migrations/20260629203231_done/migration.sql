-- CreateEnum
CREATE TYPE "AgentMemoryType" AS ENUM ('WORKING', 'EPISODIC', 'SEMANTIC', 'RETRIEVAL', 'PROCEDURAL', 'CACHE', 'COLLABORATION');

-- CreateEnum
CREATE TYPE "ModelMemoryType" AS ENUM ('CONTEXT', 'KNOWLEDGE', 'RETRIEVAL', 'PERSISTENT', 'CACHE', 'CONVERSATION');

-- CreateTable
CREATE TABLE "AgentMemory" (
    "id" SERIAL NOT NULL,
    "subAgentId" INTEGER NOT NULL,
    "type" "AgentMemoryType" NOT NULL,
    "title" TEXT,
    "content" JSONB NOT NULL,
    "importance" DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    "confidence" DOUBLE PRECISION NOT NULL DEFAULT 1,
    "tags" TEXT[],
    "embeddingId" TEXT,
    "expiresAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "AgentMemory_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ModelMemory" (
    "id" SERIAL NOT NULL,
    "subAgentId" INTEGER NOT NULL,
    "type" "ModelMemoryType" NOT NULL,
    "prompt" JSONB NOT NULL,
    "response" JSONB,
    "tokens" INTEGER,
    "summary" TEXT,
    "embeddingId" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ModelMemory_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "AgentMemory_subAgentId_idx" ON "AgentMemory"("subAgentId");

-- CreateIndex
CREATE INDEX "AgentMemory_type_idx" ON "AgentMemory"("type");

-- CreateIndex
CREATE INDEX "ModelMemory_subAgentId_idx" ON "ModelMemory"("subAgentId");

-- CreateIndex
CREATE INDEX "ModelMemory_type_idx" ON "ModelMemory"("type");

-- AddForeignKey
ALTER TABLE "AgentMemory" ADD CONSTRAINT "AgentMemory_subAgentId_fkey" FOREIGN KEY ("subAgentId") REFERENCES "SubAgent"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ModelMemory" ADD CONSTRAINT "ModelMemory_subAgentId_fkey" FOREIGN KEY ("subAgentId") REFERENCES "SubAgent"("id") ON DELETE CASCADE ON UPDATE CASCADE;
