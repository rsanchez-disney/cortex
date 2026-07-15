"""Graph diffing — compare two platform graph snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ServiceChange:
    """A single change to a service between graph snapshots."""

    service: str
    change_type: str  # "added", "removed", "modified"
    details: list[str] = field(default_factory=list)


@dataclass
class GraphDiff:
    """The full diff between two platform graph snapshots."""

    changes: list[ServiceChange]
    added_services: list[str]
    removed_services: list[str]
    modified_services: list[str]
    added_endpoints: list[dict]  # [{service, method, path}]
    removed_endpoints: list[dict]
    added_kafka_topics: list[dict]  # [{service, topic, direction}]
    removed_kafka_topics: list[dict]

    @property
    def has_changes(self) -> bool:
        return len(self.changes) > 0


def compute_diff(old_graph: dict, new_graph: dict) -> GraphDiff:
    """Compute the diff between two platform graph snapshots."""
    old_services = {s["name"]: s for s in old_graph.get("services", [])}
    new_services = {s["name"]: s for s in new_graph.get("services", [])}

    added = set(new_services) - set(old_services)
    removed = set(old_services) - set(new_services)
    common = set(old_services) & set(new_services)

    changes: list[ServiceChange] = []
    added_endpoints: list[dict] = []
    removed_endpoints: list[dict] = []
    added_kafka: list[dict] = []
    removed_kafka: list[dict] = []
    modified: list[str] = []

    for name in sorted(added):
        svc = new_services[name]
        details = [f"type: {svc.get('type', 'unknown')}"]
        ep_count = len(svc.get("endpoints", []))
        if ep_count:
            details.append(f"{ep_count} endpoints")
        changes.append(ServiceChange(service=name, change_type="added", details=details))

    for name in sorted(removed):
        changes.append(ServiceChange(service=name, change_type="removed"))

    for name in sorted(common):
        old_svc = old_services[name]
        new_svc = new_services[name]
        diffs: list[str] = []

        # Compare endpoints
        old_eps = {
            (e.get("method", ""), e.get("path", ""))
            for e in old_svc.get("endpoints", [])
        }
        new_eps = {
            (e.get("method", ""), e.get("path", ""))
            for e in new_svc.get("endpoints", [])
        }
        for method, path in sorted(new_eps - old_eps):
            diffs.append(f"+ endpoint {method} {path}")
            added_endpoints.append({"service": name, "method": method, "path": path})
        for method, path in sorted(old_eps - new_eps):
            diffs.append(f"- endpoint {method} {path}")
            removed_endpoints.append({"service": name, "method": method, "path": path})

        # Compare Kafka produces
        old_produces = set(old_svc.get("kafka_produces", []))
        new_produces = set(new_svc.get("kafka_produces", []))
        for topic in sorted(new_produces - old_produces):
            diffs.append(f"+ produces {topic}")
            added_kafka.append({"service": name, "topic": topic, "direction": "produces"})
        for topic in sorted(old_produces - new_produces):
            diffs.append(f"- produces {topic}")
            removed_kafka.append({"service": name, "topic": topic, "direction": "produces"})

        # Compare Kafka consumes
        old_consumes = set(old_svc.get("kafka_consumes", []))
        new_consumes = set(new_svc.get("kafka_consumes", []))
        for topic in sorted(new_consumes - old_consumes):
            diffs.append(f"+ consumes {topic}")
            added_kafka.append({"service": name, "topic": topic, "direction": "consumes"})
        for topic in sorted(old_consumes - new_consumes):
            diffs.append(f"- consumes {topic}")
            removed_kafka.append({"service": name, "topic": topic, "direction": "consumes"})

        # Compare dependencies count (summary only)
        old_dep_count = len(old_svc.get("dependencies", []))
        new_dep_count = len(new_svc.get("dependencies", []))
        if abs(new_dep_count - old_dep_count) > 2:
            diffs.append(f"dependencies: {old_dep_count} → {new_dep_count}")

        if diffs:
            modified.append(name)
            changes.append(ServiceChange(service=name, change_type="modified", details=diffs))

    return GraphDiff(
        changes=changes,
        added_services=sorted(added),
        removed_services=sorted(removed),
        modified_services=sorted(modified),
        added_endpoints=added_endpoints,
        removed_endpoints=removed_endpoints,
        added_kafka_topics=added_kafka,
        removed_kafka_topics=removed_kafka,
    )
