"""Compile validated records into a deliberately small Mermaid grammar.

Never accept executable diagram syntax from a model. The graph remains the
source of truth when labels need repair, views change, or branches are folded.
"""

from __future__ import annotations

import re

from .models import GraphView, PaperAnalysis, PaperEdge


def escape_label(value: str, limit: int = 36, *, conservative: bool = False) -> str:
    value = value[:limit] + ("…" if len(value) > limit else "")
    if conservative:
        value = re.sub(r"[^\w\s\u3400-\u9fff,.，。:：、()（）%+−–-]", " ", value)
    return re.sub(r'[&"<>\n\r\[\]{}|`]', lambda m: f"#{ord(m[0])};", value)


def compile_mermaid(
    analysis: PaperAnalysis,
    node_ids: list[str],
    edge_ids: list[str],
    *,
    conservative: bool = False,
    show_edge_labels: bool = True,
    tree_layout: bool = True,
) -> str:
    known = set(node_ids)
    selected_edges = set(edge_ids)
    parents = {node.id: node.parent_id for node in analysis.nodes}
    lines = ["flowchart LR"]
    for node in analysis.nodes:
        if node.id in known:
            title = escape_label(node.title, conservative=conservative)
            lines.extend(
                [
                    f'n{node.id}["{title}"]',
                    f"class n{node.id} pl_{node.id};",
                    f"class n{node.id} kind_{node.kind};",
                ]
            )
    for edge in analysis.edges:
        if (
            edge.id in selected_edges
            and edge.source in known
            and edge.target in known
            and (not tree_layout or parents.get(edge.target) == edge.source)
        ):
            arrow = "-.->" if edge.relation == "limits" else "-->"
            label = (
                escape_label(edge.label, 14, conservative=conservative) if show_edge_labels else ""
            )
            annotation = f'|"{label}"|' if label else ""
            lines.append(f"n{edge.source} {arrow}{annotation} n{edge.target}")
    if tree_layout:
        lines.append(
            "%% Cross-relations remain in report.analysis.edges and the interactive node view."
        )
    return "\n".join(lines) + "\n"


def build_views(analysis: PaperAnalysis) -> list[GraphView]:
    parents = {n.id: n.parent_id for n in analysis.nodes}
    connections = {(e.source, e.target) for e in analysis.edges}
    existing_edge_ids = {e.id for e in analysis.edges}
    for node in analysis.nodes:
        if node.parent_id and (node.parent_id, node.id) not in connections:
            edge_id = f"hierarchy_{node.id}"
            while edge_id in existing_edge_ids:
                edge_id += "_"
            existing_edge_ids.add(edge_id)
            analysis.edges.append(PaperEdge(id=edge_id, source=node.parent_id, target=node.id))

    def ancestors(ids: set[str]) -> set[str]:
        result = set(ids)
        for node_id in ids:
            current = parents.get(node_id)
            while current:
                result.add(current)
                current = parents.get(current)
        return result

    def depth(node_id: str) -> int:
        result, current = 0, parents[node_id]
        while current:
            result += 1
            current = parents[current]
        return result

    all_ids = {n.id for n in analysis.nodes}
    overview = {n.id for n in analysis.nodes if n.parent_id is None}
    priority = {
        "question": 8,
        "contribution": 9,
        "conclusion": 7,
        "limitation": 6,
        "method": 5,
        "evidence": 3,
        "concept": 1,
        "background": 0,
    }
    ranked = sorted(
        analysis.nodes,
        key=lambda n: (
            depth(n.id),
            -priority.get(n.kind, 0),
            -len(n.evidence_ids),
        ),
    )
    # Preserve the paper's shallow argument branches before expanding individual
    # categories; several detailed limitations must not crowd out the method.
    # A readable entry view contains a bounded, connected subset. The complete
    # argument view always retains every node and every logical relationship.
    for node in ranked:
        proposed = overview | ancestors({node.id})
        if len(proposed) <= 9:
            overview = proposed
    focus = (
        ancestors(
            {
                n.id
                for n in analysis.nodes
                if n.kind
                in {
                    "contribution",
                    "evidence",
                    "conclusion",
                    "limitation",
                }
            }
        )
        or all_ids
    )
    definitions = [
        ("overview", "研究全景", "从主要问题与研究路线进入，细节可在完整论证中查看。", overview),
        ("argument", "完整论证", "全部论点及其支持、推导与限制关系。", all_ids),
        ("evidence", "贡献与证据", "关注结论的依据、主要贡献以及适用边界。", focus),
    ]
    views = []
    for view_id, title, description, ids in definitions:
        node_ids = [n.id for n in analysis.nodes if n.id in ids]
        edge_ids = [
            e.id
            for e in analysis.edges
            if e.source in ids
            and e.target in ids
            and (view_id != "overview" or parents.get(e.target) == e.source)
        ]
        views.append(
            GraphView(
                id=view_id,
                title=title,
                description=description,
                node_ids=node_ids,
                edge_ids=edge_ids,
                mermaid=compile_mermaid(
                    analysis, node_ids, edge_ids, show_edge_labels=view_id != "overview"
                ),
            )
        )
    return views
