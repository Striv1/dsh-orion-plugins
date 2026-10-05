"""Explicit wire-format parsing for full candidate graphs."""

from rdflib import Graph


def parse_materialized_graph(content: str, *, format: str = "turtle") -> Graph:
    if format not in {"nt", "turtle"}:
        raise ValueError("实例图格式只支持 nt 或 turtle。")
    return Graph().parse(data=content, format=format)
