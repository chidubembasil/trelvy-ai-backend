from fastapi import APIRouter, HTTPException, Depends
from starlette import status
import uuid
from database import db
from schema import MultiAgentDetails
from runtime.auth import get_current_user

router = APIRouter(
    prefix="/multi-agents",
    tags=["Multi Agents"],
)

#codex code
# async def codex_code(details):
#         with Codex() as codex:
#             thread = codex.thread_start(sandbox=Sandbox.workspace_write)
#             # Use the Codex API to generate code based on the prompt and output
#             generated_code = thread.run(details.prompt)
#             return generated_code

# Create a new multi-agent for the logged in user
@router.post("/", response_model=MultiAgentDetails, status_code=status.HTTP_201_CREATED)
async def create_multi_agent(
    agent_details: MultiAgentDetails,
    current_user=Depends(get_current_user)
):
    new_agent = await db.multiAgent.create(
        data={
            "uniqueId":     str(uuid.uuid4()),
            "name":         agent_details.name,
            "prompt":       agent_details.prompt,
            "filePath":     agent_details.filePath,
            "version":      agent_details.version,
            "status":       agent_details.status,
            "codingAgent":  agent_details.codingAgent,
            "userId":       current_user.id,
        }
    )
    await codex_code(agent_details.prompt)
    if not new_agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create multi-agent"
        )
    else:
        raise HTTPException(status_code=200, detail="Multi-Agent is Created")
    return new_agent

    

# Get all multi-agents for the logged in user only
@router.get("/", response_model=list[MultiAgentDetails], status_code=status.HTTP_200_OK)
async def get_all_multi_agents(
    current_user=Depends(get_current_user)  # gets the logged in user
):
    agents = await db.multiAgent.find_many(
        where={"userId": current_user.id}   # only return this user's agents
    )
    return agents


# Get a single multi-agent by uniqueId (only if it belongs to the user)
@router.get("/{unique_id}", response_model=MultiAgentDetails, status_code=status.HTTP_200_OK)
async def get_multi_agent(
    unique_id: str,
    current_user=Depends(get_current_user)
):
    agent = await db.multiAgent.find_first(
        where={
            "uniqueId": unique_id,
            "userId":   current_user.id  # make sure it belongs to this user
        }
    )
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )

    return agent


# Delete a multi-agent (only if it belongs to the user)
@router.delete("/{unique_id}", status_code=status.HTTP_200_OK)
async def delete_multi_agent(
    unique_id: str,
    current_user=Depends(get_current_user)
):
    agent = await db.multiAgent.find_first(
        where={
            "uniqueId": unique_id,
            "userId":   current_user.id
        }
    )
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found or doesn't belong to you"
        )
    await db.multiAgent.delete(where={"uniqueId": unique_id})
    return {"detail": "Agent deleted successfully"}