"""One planner entry point shared by initial plans and conversation edits."""

from functools import lru_cache

from ..config import settings


class WorkflowPlanner:
    """Keep conversational regeneration on the same graph as /trip/plan."""

    def plan_trip(self, request):
        from ..workflows.langgraph_trip_workflow import get_langgraph_trip_workflow

        plan, _ = get_langgraph_trip_workflow().run(request)
        return plan

    def replan(self, request):
        from ..agents.multi_agent_orchestrator import get_multi_agent_orchestrator

        return get_multi_agent_orchestrator().replan(request)

    def health_snapshot(self):
        return {"status": "healthy", "service": "trip-planner", "mode": "langgraph"}


@lru_cache(maxsize=1)
def get_planner():
    if settings.workflow_mode.strip().lower() == "langgraph":
        return WorkflowPlanner()
    if settings.workflow_mode.strip().lower() == "legacy":
        from ..agents.multi_agent_orchestrator import get_multi_agent_orchestrator

        return get_multi_agent_orchestrator()

    from ..agents.trip_planner_agent import MultiAgentTripPlanner

    return MultiAgentTripPlanner(lightweight=True)
