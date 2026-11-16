"""Generic, read-only investigation tools backed by ingested DuckDB data."""

from incidents_agent.tools import documents
from incidents_agent.tools import graph
from incidents_agent.tools import logs
from incidents_agent.tools import metrics
from incidents_agent.tools import registry
from incidents_agent.tools import safety
from incidents_agent.tools import schema
from incidents_agent.tools import tables

ToolRegistry = registry.ToolRegistry
create_default_registry = registry.create_default_registry

__all__ = [
    'ToolRegistry',
    'create_default_registry',
    'documents',
    'graph',
    'logs',
    'metrics',
    'registry',
    'safety',
    'schema',
    'tables',
]
