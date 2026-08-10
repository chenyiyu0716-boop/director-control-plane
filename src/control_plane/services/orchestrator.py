from typing import Dict

from ..agents import KnowledgeAgent, ObserverAgent, ReleaseAgent
from ..config import ProjectConfig
from ..domain.models import AgentType, RunStatus
from ..storage import Repository


class Orchestrator:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.agents: Dict[AgentType, object] = {
            AgentType.KNOWLEDGE: KnowledgeAgent(),
            AgentType.OBSERVER: ObserverAgent(),
            AgentType.RELEASE: ReleaseAgent(),
        }

    def run(self, project: ProjectConfig, agent_type: AgentType, trigger: str = "manual") -> str:
        self.repository.upsert_project(project)
        run_id = self.repository.start_run(project.id, agent_type.value, trigger)
        try:
            result = self.agents[agent_type].run(project)
            for finding in result.findings:
                self.repository.add_finding(run_id, finding)
            for check in result.checks:
                self.repository.add_check(run_id, check)
            for candidate in result.candidates:
                self.repository.add_candidate(run_id, project.id, candidate)
            if result.release_report:
                self.repository.add_release_report(run_id, result.release_report)
            self.repository.finish_run(run_id, result.status, result.summary)
        except Exception as error:
            self.repository.finish_run(run_id, RunStatus.FAILED, "Agent 执行失败。", {"type": type(error).__name__, "message": str(error)[:500]})
            raise
        return run_id
