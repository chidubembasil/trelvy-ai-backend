-- CreateEnum
CREATE TYPE "CodingAgent" AS ENUM ('CODEX', 'CLAUDE_CODE', 'JULES');

-- CreateTable
CREATE TABLE "User" (
    "id" SERIAL NOT NULL,
    "name" TEXT NOT NULL,
    "email" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "User_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "MultiAgent" (
    "id" SERIAL NOT NULL,
    "uniqueId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "description" TEXT,
    "filePath" TEXT NOT NULL,
    "version" TEXT NOT NULL DEFAULT '1.0.0',
    "status" TEXT NOT NULL DEFAULT 'active',
    "codingAgent" "CodingAgent" NOT NULL DEFAULT 'CODEX',
    "userId" INTEGER NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "MultiAgent_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "SubAgent" (
    "id" SERIAL NOT NULL,
    "uniqueId" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "filePath" TEXT NOT NULL,
    "parentId" INTEGER NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "SubAgent_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "User_email_key" ON "User"("email");

-- CreateIndex
CREATE UNIQUE INDEX "MultiAgent_uniqueId_key" ON "MultiAgent"("uniqueId");

-- CreateIndex
CREATE UNIQUE INDEX "SubAgent_uniqueId_key" ON "SubAgent"("uniqueId");

-- AddForeignKey
ALTER TABLE "MultiAgent" ADD CONSTRAINT "MultiAgent_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "SubAgent" ADD CONSTRAINT "SubAgent_parentId_fkey" FOREIGN KEY ("parentId") REFERENCES "MultiAgent"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
