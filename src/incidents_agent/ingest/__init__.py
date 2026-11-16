"""Ingest package turning dropped incident folders into DuckDB + catalog."""

from incidents_agent.ingest import chunks
from incidents_agent.ingest import classifier
from incidents_agent.ingest import loaders
from incidents_agent.ingest import relationships
from incidents_agent.ingest import schema
from incidents_agent.ingest import time_detect

Catalog = schema.Catalog
IngestConfig = schema.IngestConfig
IngestLimits = schema.IngestLimits
IngestResult = schema.IngestResult
ingest_folder = loaders.ingest_folder

__all__ = [
    'Catalog',
    'IngestConfig',
    'IngestLimits',
    'IngestResult',
    'chunks',
    'classifier',
    'ingest_folder',
    'loaders',
    'relationships',
    'schema',
    'time_detect',
]
