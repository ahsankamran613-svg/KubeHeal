"""Background Kubernetes watcher for crashing pods with ownerReference resolution and cooldown deduplication."""

import asyncio
import logging
import time
from typing import Callable, Coroutine, Dict, Optional, Set

from kubeheal.config import settings
from kubeheal.gitops.yaml_patcher import infer_workload_name_from_pod
from kubeheal.models import AlertPayload

logger = logging.getLogger(__name__)

FAILURE_WAITING_REASONS: Set[str] = {
    "CrashLoopBackOff",
    "Error",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
}


class KubernetesWatcher:
    """Monitors Kubernetes pods in a namespace and triggers automated triage on crash events."""

    def __init__(
        self,
        namespace: Optional[str] = None,
        poll_interval: Optional[int] = None,
        cooldown_seconds: Optional[int] = None,
        on_alert_callback: Optional[Callable[[AlertPayload], Coroutine]] = None,
    ):
        self.namespace = namespace or settings.kube_namespace
        self.poll_interval = poll_interval or settings.watcher_poll_interval_seconds
        self.cooldown_seconds = cooldown_seconds or settings.alert_cooldown_seconds
        self.on_alert_callback = on_alert_callback
        self._is_running = False
        self._cooldown_cache: Dict[str, float] = {}

    def _get_core_v1(self):
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            return client.CoreV1Api()
        except Exception as e:
            logger.debug(f"K8s watcher cannot initialize client: {e}")
            return None

    def _is_in_cooldown(self, target_key: str) -> bool:
        now = time.time()
        last_triggered = self._cooldown_cache.get(target_key, 0)
        if now - last_triggered < self.cooldown_seconds:
            return True
        self._cooldown_cache[target_key] = now
        return False

    async def scan_pods_once(self):
        """Perform a single scan of pods in the namespace."""
        core_v1 = self._get_core_v1()
        if not core_v1:
            return

        try:
            pod_list = core_v1.list_namespaced_pod(namespace=self.namespace)
        except Exception as e:
            logger.debug(f"K8s watcher pod list query failed: {e}")
            return

        for pod in pod_list.items:
            pod_name = pod.metadata.name
            owner_refs = getattr(pod.metadata, "owner_references", None)
            dep_name = infer_workload_name_from_pod(pod_name, owner_references=owner_refs)
            target_key = f"{self.namespace}/{dep_name}"

            if not pod.status.container_statuses:
                continue

            for cs in pod.status.container_statuses:
                alert: Optional[AlertPayload] = None

                # Check 1: Waiting container in any failure state
                if cs.state.waiting and cs.state.waiting.reason in FAILURE_WAITING_REASONS:
                    exit_code = None
                    if cs.last_state and cs.last_state.terminated:
                        exit_code = cs.last_state.terminated.exit_code

                    alert = AlertPayload(
                        pod_name=pod_name,
                        namespace=self.namespace,
                        deployment_name=dep_name,
                        container_name=cs.name,
                        reason=cs.state.waiting.reason,
                        exit_code=exit_code,
                        restart_count=cs.restart_count,
                        message=cs.state.waiting.message or f"Pod container {cs.name} is in {cs.state.waiting.reason}",
                    )

                # Check 2: Currently terminated or last_state terminated with OOMKilled or ANY non-zero exit code
                else:
                    term = None
                    if cs.state.terminated and (cs.state.terminated.reason == "OOMKilled" or (cs.state.terminated.exit_code is not None and cs.state.terminated.exit_code != 0)):
                        term = cs.state.terminated
                    elif cs.last_state and cs.last_state.terminated:
                        t_candidate = cs.last_state.terminated
                        if t_candidate.reason == "OOMKilled" or (t_candidate.exit_code is not None and t_candidate.exit_code != 0):
                            term = t_candidate

                    if term is not None:
                        alert = AlertPayload(
                            pod_name=pod_name,
                            namespace=self.namespace,
                            deployment_name=dep_name,
                            container_name=cs.name,
                            reason=term.reason or f"ExitCode:{term.exit_code}",
                            exit_code=term.exit_code,
                            restart_count=cs.restart_count,
                            message=term.message or f"Container {cs.name} terminated with reason {term.reason} (exit code {term.exit_code})",
                        )

                if alert:
                    if self._is_in_cooldown(target_key):
                        logger.info(f"Pod {pod_name} ({alert.reason}) is in triage cooldown; skipping duplicate alert.")
                        break

                    logger.info(f"🚨 K8s Watcher detected incident on {pod_name}: {alert.reason} (exit code {alert.exit_code})")
                    if self.on_alert_callback:
                        asyncio.create_task(self.on_alert_callback(alert))
                    break

    async def start(self):
        """Run continuous background monitoring loop."""
        self._is_running = True
        logger.info(f"Starting KubeHeal Kubernetes watcher on namespace '{self.namespace}' (interval: {self.poll_interval}s)")
        while self._is_running:
            try:
                await self.scan_pods_once()
            except Exception as e:
                logger.warning(f"Error in watcher poll cycle: {e}")
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        """Stop background monitoring loop."""
        self._is_running = False
