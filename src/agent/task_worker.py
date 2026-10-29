import logging
from threading import Event, Thread
from time import monotonic

from src.agent.config import get_settings
from src.agent.runtime import run_chat, run_plan_action
from src.services import executions, tasks, plan_actions


logger = logging.getLogger(__name__)


class TaskWorker:
    def __init__(self, workers: int | None = None):
        self.workers = workers or get_settings().task_workers
        self.stop_requested = Event()
        self.threads = []

    def run_once(self) -> bool:
        plan_actions.schedule_ready(queue_name=tasks.get_settings().task_queue_name)
        item = tasks.claim_next()
        if item is None:
            return False
        try:
            role = executions.current_role(item.employee_id)
            run_id = item.run_id or item.task_id
            if item.operation == "plan_action":
                result = run_plan_action(item.thread_id, run_id, item.employee_id, role, item.task_id,
                                         item.command_id, item.worker_token)
            else:
                result = run_chat(item.message, str(item.thread_id), str(run_id), item.employee_id,
                                   role=role, skill_id=item.skill_id, task_id=item.task_id, worker_token=item.worker_token)
            tasks.record_outcome(item.task_id, run_id, result=result, worker_token=item.worker_token)
        except Exception as exc:
            logger.warning("Background Agent task failed (%s).", type(exc).__name__)
            tasks.record_outcome(item.task_id, item.run_id or item.task_id, error=exc, worker_token=item.worker_token)
            if item.operation == "plan_action" and item.command_id:
                plan_actions.finish(item.command_id, "failed", error_code="execution_failed")
        return True

    def _loop(self):
        while not self.stop_requested.is_set():
            try:
                tasks.recover_expired()
                if self.stop_requested.is_set():
                    break
                if not self.run_once():
                    self.stop_requested.wait(get_settings().task_poll_seconds)
            except Exception as exc:
                logger.warning("Background worker unavailable (%s).", type(exc).__name__)
                self.stop_requested.wait(max(1, get_settings().task_poll_seconds))

    def start(self):
        for index in range(self.workers):
            thread = Thread(target=self._loop, name=f"chexu-agent-worker-{index}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self, grace_seconds: float = 30) -> bool:
        # 停止领取新任务；未能在宽限时间结束的执行由检查点和租约恢复机制接管。
        self.stop_requested.set()
        deadline = monotonic() + grace_seconds
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - monotonic()))
        stopped = all(not thread.is_alive() for thread in self.threads)
        if not stopped:
            logger.warning("Worker shutdown grace period elapsed; unfinished tasks retain checkpoints.")
        return stopped
