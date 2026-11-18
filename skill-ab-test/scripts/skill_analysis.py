"""Validate main-authored skill anatomy against pinned source and public traces.

This module checks evidence integrity and graph references. It does not infer
file roles, reconstruct behavior, or evaluate the quality of either response.
"""
from __future__ import annotations

from pathlib import Path
import re

from report_lib import digest, local_path, need, obj, read_json, read_text, seq, string


FILE_KINDS = {'instruction', 'reference', 'script', 'asset', 'metadata', 'license', 'other'}
TOOL_KINDS = {'service', 'runtime', 'dependency'}
ARMS = {'baseline', 'treatment'}
TOOL_EVENT_TYPES = {'command_execution', 'mcp_tool_call', 'web_search', 'file_change'}


def load_analysis(run):
    """Return optional, validated ``skill-analysis.json`` without changing it.

    ``run`` is the validated trace-report run mapping. Every inventory file must
    be in its pinned source set; every quoted source range or observed event is
    checked independently. An absent analysis preserves older reports.
    """
    # Import lazily: trace_report may call this module while loading a report.
    from trace_report import validate_evidence

    root = Path(run['root']).resolve()
    path = local_path(root, 'skill-analysis.json')
    if not path.exists():
        return None
    data = read_json(path)
    need(type(data.get('schema_version')) is int and data['schema_version'] == 1,
         'analysis.schema_version must be 1')
    author = obj(data.get('analyzed_by'), 'analysis.analyzed_by')
    need(author.get('role') == 'main', 'only the main agent may author skill analysis')
    string(author.get('agent_id'), 'analysis.analyzed_by.agent_id')
    summary = obj(data.get('summary'), 'analysis.summary')
    for key in ('purpose', 'activation', 'input', 'output', 'mechanism'):
        string(summary.get(key), f'analysis.summary.{key}')
    inventory = obj(data.get('inventory'), 'analysis.inventory')
    for key in ('scope', 'basis'):
        string(inventory.get(key), f'analysis.inventory.{key}')

    inputs = obj(run['record'].get('inputs_sha256'), 'inputs_sha256')
    pinned = {relative: sha for relative, sha in inputs.items() if relative.startswith('source/')}
    need(bool(pinned), 'analysis requires pinned source files')
    file_paths = set()
    files = seq(data.get('files'), 'analysis.files')
    for index, entry in enumerate(files):
        label = f'analysis.files[{index}]'
        obj(entry, label)
        relative = string(entry.get('path'), f'{label}.path')
        source_path = local_path(root, relative)
        need(relative in pinned, f'{label}: file is outside pinned source inventory')
        need(relative not in file_paths, f'{label}: duplicate inventory file')
        file_paths.add(relative)
        need(digest(source_path) == pinned[relative], f'{label}: pinned source hash mismatch')
        for key in ('role', 'when'):
            string(entry.get(key), f'{label}.{key}')
        need(entry.get('kind') in FILE_KINDS, f'{label}: unsupported file kind')
        for linked in seq(entry.get('links'), f'{label}.links'):
            string(linked, f'{label}.links item')
            local_path(root, linked)
            need(linked in pinned, f'{label}: linked file is outside pinned source inventory')
    need(file_paths == set(pinned), 'analysis.files must cover every pinned source file exactly once')

    source_lines = {}

    def check_sources(refs, label):
        seq(refs, label)
        for index, ref in enumerate(refs):
            ref_label = f'{label}[{index}]'
            obj(ref, ref_label)
            relative = string(ref.get('path'), f'{ref_label}.path')
            source_path = local_path(root, relative)
            need(relative in pinned, f'{ref_label}: source is not in pinned inventory')
            if relative not in source_lines:
                source_lines[relative] = read_text(source_path).splitlines(keepends=True)
            lines = source_lines[relative]
            start, end = ref.get('start'), ref.get('end')
            need(type(start) is int and type(end) is int and 1 <= start <= end <= len(lines),
                 f'{ref_label}: valid inclusive source line range required')
            quote = string(ref.get('quote'), f'{ref_label}.quote')
            need(quote in ''.join(lines[start - 1:end]),
                 f'{ref_label}: quote differs from source line range')

    def check_observations(observations, label):
        seq(observations, label)
        for index, observation in enumerate(observations):
            ref_label = f'{label}[{index}]'
            obj(observation, ref_label)
            arm = observation.get('arm')
            need(arm in ARMS, f'{ref_label}: known observation arm required')
            need(observation.get('kind') in {'event', 'answer'},
                 f'{ref_label}: public event or answer evidence required')
            validate_evidence(observation, run['arms'][arm], ref_label)

    flow = seq(data.get('flow'), 'analysis.flow')
    need(bool(flow), 'analysis.flow must contain at least one sourced step')
    identifiers = set()
    for index, step in enumerate(flow):
        label = f'analysis.flow[{index}]'
        obj(step, label)
        identifier = string(step.get('id'), f'{label}.id')
        need(re.fullmatch(r'[a-z][a-z0-9-]*', identifier) is not None,
             f'{label}: safe step id required')
        need(identifier not in identifiers, f'{label}: duplicate step id')
        identifiers.add(identifier)
        for key in ('label', 'condition', 'instruction', 'observation_note'):
            string(step.get(key), f'{label}.{key}')
        check_sources(step.get('source'), f'{label}.source')
        need(bool(step['source']), f'{label}: source evidence required')
        check_observations(step.get('observations'), f'{label}.observations')
        seq(step.get('next'), f'{label}.next')
    for index, step in enumerate(flow):
        for target in step['next']:
            string(target, f'analysis.flow[{index}].next item')
            need(target in identifiers, f'analysis.flow[{index}]: next step does not exist')

    for index, tool in enumerate(seq(data.get('tools'), 'analysis.tools')):
        label = f'analysis.tools[{index}]'
        obj(tool, label)
        for key in ('name', 'purpose', 'availability', 'observation_note'):
            string(tool.get(key), f'{label}.{key}')
        need(tool.get('kind') in TOOL_KINDS, f'{label}: unsupported tool kind')
        check_sources(tool.get('source'), f'{label}.source')
        check_observations(tool.get('observations'), f'{label}.observations')
        need(bool(tool['source'] or tool['observations']), f'{label}: source or observed evidence required')
        if tool['kind'] == 'runtime':
            runtime_events = [run['arms'][observation['arm']]['by_line'][observation['line']]['event']
                              for observation in tool['observations'] if observation['kind'] == 'event']
            need(any(event.get('type') in {'item.started', 'item.completed'}
                     and isinstance(event.get('item'), dict)
                     and event['item'].get('type') in TOOL_EVENT_TYPES
                     for event in runtime_events),
                 f'{label}: runtime tool requires an observed tool event')
        else:
            need(bool(tool['source']), f'{label}: declared service or dependency requires source evidence')
    for note in seq(data.get('scope_notes'), 'analysis.scope_notes'):
        string(note, 'analysis.scope_notes item')
    return data
