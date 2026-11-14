#!/usr/bin/env python3
"""Offline acceptance against real persisted recordings and a real Hermes host.

No provider calls, invented turns, stamps, model outputs, or host substitutes.
The input is an operator-owned JSON export, never a committed fixture.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--records', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    core = args.core.resolve(strict=True)
    plugin = Path(__file__).resolve().parents[1]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['HERMES_HOME'] = str(out / 'home')
    os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
    sys.path.insert(0, str(core))
    network_attempts=[]
    def audit(event, arguments):
        if event=='socket.connect':
            network_attempts.append(str(arguments[1]))
            raise RuntimeError('Recorded-host acceptance must not open a network connection')
    sys.addaudithook(audit)
    spec = importlib.util.spec_from_file_location('hermes_lcm', plugin / '__init__.py', submodule_search_locations=[str(plugin)])
    sys.modules['hermes_lcm'] = importlib.util.module_from_spec(spec)
    from agent.context_engine import ContextEngine
    from agent.message_sanitization import native_anthropic_row_costs
    from agent.replayed_reasoning import codex_reasoning_replay_tokens
    from agent.anthropic_message_convert import convert_messages_to_anthropic
    from gateway.run import _build_replay_entry
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.db_bootstrap import classify_version_mismatch, VERSION_MISMATCH_INTERIM_STAMP
    from hermes_lcm.dag import SummaryDAG, SummaryNode
    from hermes_lcm.engine import LCMEngine, _strip_replay_metadata_for_recovery_message
    from hermes_lcm.event_time import read_event_time, scope_time_status
    from hermes_lcm.escalation import _build_l1_prompt, _build_l2_prompt
    from hermes_lcm.rollup_builder import _daily_sources
    from hermes_lcm.tokens import _count_payload_tokens, count_messages_tokens, count_tokens, message_token_costs
    from hermes_lcm.tools import lcm_recent
    from hermes_lcm.store import MessageStore
    assert Path(inspect.getfile(ContextEngine)).resolve() == core / 'agent/context_engine.py'
    assert Path(inspect.getfile(native_anthropic_row_costs)).resolve() == core / 'agent/message_sanitization.py'

    payload = args.records.read_bytes()
    recorded = json.loads(payload)
    def replay(rows):
        messages = []
        for raw in rows:
            message = {k:raw[k] for k in ('role','content','tool_call_id','tool_name','api_content','reasoning','reasoning_content','timestamp') if raw.get(k) is not None}
            for key in ('tool_calls','reasoning_details','codex_reasoning_items','codex_message_items','display_metadata'):
                if raw.get(key) is not None:
                    message[key] = json.loads(raw[key]) if isinstance(raw[key],str) else raw[key]
            message['_row_id'] = raw['id']
            messages.append(message)
        return messages
    messages = replay(recorded['core_rows'])
    signed = replay(recorded['signed_rows'])
    originals = copy.deepcopy(messages)
    assert any(codex_reasoning_replay_tokens(m) for m in messages)
    assert any(m.get('reasoning_details') for m in signed)
    receipt = {'core_sha':subprocess.check_output(['git','-C',str(core),'rev-parse','HEAD'],text=True).strip(), 'lcm_sha':subprocess.check_output(['git','-C',str(plugin),'rev-parse','HEAD'],text=True).strip(), 'recordings_sha256':hashlib.sha256(payload).hexdigest(), 'host':str(core), 'provider_calls':0, 'checks':{}}
    receipt['python']=sys.version
    receipt['core_dirty_paths']=subprocess.check_output(['git','-C',str(core),'status','--short','--untracked-files=no'],text=True).splitlines()
    receipt['host_source_sha256']={p:hashlib.sha256((core/p).read_bytes()).hexdigest() for p in ('agent/context_engine.py','agent/message_sanitization.py','agent/replayed_reasoning.py','agent/message_metadata.py','agent/anthropic_message_convert.py','gateway/run.py')}
    changed = subprocess.check_output(['git','-C',str(plugin),'diff','--name-only'],text=True).splitlines()
    changed += subprocess.check_output(['git','-C',str(plugin),'ls-files','--others','--exclude-standard'],text=True).splitlines()
    receipt['working_source_sha256'] = {p:hashlib.sha256((plugin/p).read_bytes()).hexdigest() for p in sorted(set(changed)) if (plugin/p).is_file()}
    checks = receipt['checks']
    for name, rows in (('responses',messages),('native_anthropic',signed)):
        projections = native_anthropic_row_costs(rows)
        expected = sum(max(_count_payload_tokens(shadow) + thinking,_count_payload_tokens(raw,include_reasoning_content=False) + codex_reasoning_replay_tokens(raw)) for raw,(shadow,thinking) in zip(rows,projections,strict=True))
        assert count_messages_tokens(rows) == expected
        stripped = [_strip_replay_metadata_for_recovery_message(row) for row in rows]
        assert count_messages_tokens(stripped) == sum(_count_payload_tokens(row) for row in stripped)
        assert all(codex_reasoning_replay_tokens(row)==0 for row in stripped)
        checks[name] = {'rows':len(rows),'tokens':expected,'recovery_tokens':count_messages_tokens(stripped),'per_row_costs':message_token_costs(rows)}
    for rows in (messages,signed):
        cold=[]
        for row in rows:
            if row.get('role')=='tool' or row.get('tool_calls'):
                cold.append(copy.deepcopy(row))
            else:
                cold.append(_build_replay_entry(row['role'],row.get('content'),copy.deepcopy(row),preserve_timestamp=True))
        assert count_messages_tokens(cold)==count_messages_tokens(rows)
        for before,after in zip(rows,cold,strict=True):
            assert before.get('display_metadata')==after.get('display_metadata')
            assert before.get('_row_id')==after.get('_row_id')
    checks['real_gateway_cold_entry_replay']=True
    convert_messages_to_anthropic(signed, model='claude-opus-4-6')
    checks['native_conversion_executed'] = True
    checks['native_signed_carrier_recordings'] = sum(any(
        isinstance(item,dict) and (item.get('signature') or item.get('type') in {'reasoning.encrypted','redacted_thinking'})
        for item in row.get('reasoning_details') or []
    ) for row in signed)
    checks['native_carrier_limit'] = 'Native conversion uses real readable reasoning records; signed-carrier acceptance requires actual signed recordings.'
    checks['exact_stamp_recordings'] = sum(bool((m.get('display_metadata') or {}).get('reasoning_tokens')) for m in messages+signed)
    checks['exact_stamp_limit'] = 'No exact-stamp acceptance is claimed when recordings contain no provider-reported stamp.'

    config=LCMConfig(database_path=str(out / 'assembly.db'), fresh_tail_count=32, fresh_tail_max_tokens=32768, max_assembly_tokens=100000, temporal_rollups_enabled=False)
    engine=LCMEngine(config=config,hermes_home=str(out/'home'))
    engine._session_id=recorded['core_rows'][0]['session_id']
    engine._foreground_session_id=engine._session_id
    engine.api_mode='codex_responses'
    engine.context_length=200000
    costs=[]
    for rows in (messages,signed):
        engine.api_mode='codex_responses' if rows is messages else 'anthropic_messages'
        budget=count_messages_tokens(rows,api_mode=engine.api_mode)
        cache={}
        for index in range(len(rows)):
            assert count_messages_tokens(rows[index:],api_mode=engine.api_mode,_payload_cache=cache)==count_messages_tokens(rows[index:],api_mode=engine.api_mode)
        started=time.perf_counter()
        selected=engine._select_atomic_tail_groups(rows,budget)
        costs.append(time.perf_counter()-started)
        assert count_messages_tokens(selected,api_mode=engine.api_mode)<=budget
        assert any(m.get('role')=='user' for m in selected)
        engine._config.max_assembly_tokens=100000
        normal=engine._assemble_context(None,rows,include_lcm_note=False)
        assert normal and any(m.get('role')=='user' for m in normal)
        assert count_messages_tokens(normal,api_mode=engine.api_mode)<=engine._effective_assembly_token_cap()
        assert any(m.get('codex_reasoning_items') or m.get('reasoning_details') for m in normal)
        engine._config.max_assembly_tokens=max(1000,budget//2)
        limited=engine._assemble_context(None,rows,include_lcm_note=False)
        assert limited and any(m.get('role')=='user' for m in limited)
        assert count_messages_tokens(limited,api_mode=engine.api_mode)<=engine._config.max_assembly_tokens
        checks.setdefault('bounded_ordinary_assembly',[]).append({'input_rows':len(rows),'output_rows':len(limited),'cap':engine._config.max_assembly_tokens,'tokens':count_messages_tokens(limited,api_mode=engine.api_mode)})
        for message in selected:
            if message.get('role')=='assistant' and message.get('tool_calls'):
                original_index=next(i for i,m in enumerate(rows) if m is message)
                for result in rows[original_index+1:]:
                    if result.get('role')!='tool':
                        break
                    assert any(m is result for m in selected)
        cap=max(count_messages_tokens(rows),1000)
        first=engine._assemble_context(None,rows,assembly_cap_override=cap,include_lcm_note=False)
        second=engine._assemble_overflow_recovery_context(None,rows,assembly_cap_override=cap)
        for recovered in (first,second):
            assert recovered and any(m.get('role')=='user' for m in recovered)
            assert count_messages_tokens(recovered)<=cap
            assert all(not m.get('codex_reasoning_items') and not m.get('reasoning_details') and not (m.get('display_metadata') or {}).get('reasoning_tokens') for m in recovered)
    assert messages==originals
    checks['atomic_selection_seconds']=costs
    checks['both_recovery_entries']=True
    checks['ordinary_final_assembly_retains_carriers']=True
    engine.shutdown()

    db=out/'event-time.db'
    store=MessageStore(db)
    for raw in recorded['store_rows']:
        columns=list(raw)
        store._conn.execute('INSERT INTO messages ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(raw[k] for k in columns))
    store._conn.commit()
    dag=SummaryDAG(db)
    raw_node=recorded['summary_nodes'][0]
    # Install one exact legacy summary into the offline candidate. Its old
    # timestamp fields must NEVER acquire event-time certification on reopen.
    columns=list(raw_node)
    dag.connection.execute('INSERT INTO summary_nodes ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(raw_node[k] for k in columns))
    dag.connection.commit()
    assert read_event_time(dag.connection,raw_node['node_id']).legacy_count==1
    assert classify_version_mismatch(dag.connection)==VERSION_MISMATCH_INTERIM_STAMP
    checks['additive_schema_classified']=True
    known=[r for r in recorded['store_rows'] if r['observed_at'] is not None]
    unknown=[r for r in recorded['store_rows'] if r['observed_at'] is None]
    assert known and unknown
    assert any(r['timestamp']-r['observed_at']>86400 for r in known)
    def publish(rows):
        # Publish an exact extract of THESE source rows. No model output or
        # unrelated historical summary is assigned to this source lineage.
        extract='\n\n'.join(str(row['content'] or '') for row in rows)
        node=SummaryNode(session_id=rows[0]['session_id'],summary=extract,source_ids=[r['store_id'] for r in rows],source_type='messages',created_at=raw_node['created_at'])
        snap=dag.publication_snapshot(node.source_ids,'messages')
        dag.publish_node(node,snap)
        return node
    complete=publish(known)
    missing=publish(unknown)
    mixed=publish(known+unknown)
    assert complete.event_time.complete
    assert missing.event_time.earliest_at is None
    assert mixed.event_time.as_dict()['status']=='mixed'
    parent=SummaryNode(session_id=complete.session_id,depth=1,summary=complete.summary+'\n\n'+missing.summary,source_ids=[complete.node_id,missing.node_id],source_type='nodes',created_at=raw_node['created_at'])
    dag.publish_node(parent,dag.publication_snapshot(parent.source_ids,'nodes'))
    assert parent.event_time.as_dict()==mixed.event_time.as_dict()
    first_date=known[0]['observed_at']
    found=store.get_session_messages(known[0]['session_id'])
    assert len(found)==len(recorded['store_rows'])
    loaded=store.load_session_page(known[0]['session_id'],time_from=complete.event_time.earliest_at,time_to=complete.event_time.latest_at)
    assert {row['store_id'] for row in loaded}=={row['store_id'] for row in known}
    assert store.count_session_load_messages(known[0]['session_id'],time_from=complete.event_time.earliest_at,time_to=complete.event_time.latest_at)==len(known)
    ingested_min=min(row['ingested_at'] or row['timestamp'] for row in known)
    assert not store.load_session_page(known[0]['session_id'],time_from=ingested_min)
    checks['raw_filters_use_event_time']=True
    from datetime import datetime, timezone
    from hermes_lcm.rollup_periods import parse_recent_period
    from hermes_lcm.tools import _recent_leaf_sections
    engine=LCMEngine(config=LCMConfig(database_path=str(db)),hermes_home=str(out/'home'))
    engine._session_id=known[0]['session_id']
    engine._foreground_session_id=engine._session_id
    serialized=engine._serialize_messages(found)
    assert 'Source store_id=' in serialized and 'event_time=unknown' in serialized
    for row in known:
        assert datetime.fromtimestamp(row['observed_at'],tz=timezone.utc).isoformat() in serialized
    checks['serialized_sources_preserve_event_dates']=True
    period='date:'+datetime.fromtimestamp(first_date,tz=timezone.utc).date().isoformat()
    sections=_recent_leaf_sections(engine,parse_recent_period(period),'conversation',100)
    assert sections and all(s['earliest_at'] is not None for s in sections)
    assert raw_node['node_id'] not in {s['node_id'] for s in sections}
    recent=json.loads(lcm_recent({'period':period},engine=engine))
    assert 'error' not in recent
    assert recent['event_time_coverage']['unknown_summary_nodes'] > 0
    checks['event_time']={'complete':complete.event_time.as_dict(),'mixed':mixed.event_time.as_dict(),'unknown':missing.event_time.as_dict(),'scope':scope_time_status(dag.connection,[complete.session_id]),'recent_nodes':[s['node_id'] for s in sections],'tool_returned_error':recent.get('error')}
    try:
        _daily_sources(dag,complete.session_id,datetime.fromtimestamp(first_date,tz=timezone.utc).date())
    except ValueError as exc:
        assert 'unknown or mixed' in str(exc)
        checks['mixed_rollup_blocked']=True
    else:
        raise AssertionError('Mixed event-time scope must not certify a daily rollup')
    # Prompt contract over recorded prose, without paying for or inventing model output.
    for depth in (0,1,2,4):
        for prompt in (_build_l1_prompt(raw_node['summary'],1500,depth,source_provenance={'node_ids':[raw_node['node_id']]}),_build_l2_prompt(raw_node['summary'],800,source_depth=depth,source_provenance={'node_ids':[raw_node['node_id']]})):
            system=prompt[0]['content']
            assert 'UNVERIFIED:' in system and '<basis>' in system and 'withdrawal' in system and 'Historical permission' in system
            envelope=json.loads(prompt[1]['content'])
            assert envelope['sources'][0]['content']==raw_node['summary']
    checks['summary_prompts_all_depths']=True
    checks['model_summary_outputs']='Not exercised; no model/provider selected or invoked.'
    dag.close()
    store.close()
    engine.shutdown()
    reopened=SummaryDAG(db)
    assert reopened.get_node(complete.node_id).event_time.complete
    assert reopened.get_node(raw_node['node_id']).event_time.legacy_count==1
    reopened.close()
    checks['reopen_preserves_new_and_legacy']=True
    # Use a copy containing only the actually recorded known-time subset to
    # verify UTC-day membership without inventing messages or event dates.
    daily_db=out/'daily-known.db'
    with sqlite3.connect(db) as source, sqlite3.connect(daily_db) as destination:
        source.backup(destination)
    daily_dag=SummaryDAG(daily_db)
    with daily_dag.connection:
        daily_dag.connection.execute('DELETE FROM summary_nodes WHERE node_id != ?', (complete.node_id,))
    first=datetime.fromtimestamp(complete.event_time.earliest_at,tz=timezone.utc).date()
    last=datetime.fromtimestamp(complete.event_time.latest_at,tz=timezone.utc).date()
    from datetime import timedelta
    days=[]
    current=first
    while current<=last:
        assert [s['node_id'] for s in _daily_sources(daily_dag,complete.session_id,current)]==[complete.node_id]
        days.append(current.isoformat())
        current+=timedelta(days=1)
    checks['known_rollup_utc_days']=days
    from hermes_lcm.rollup_store import RollupStore
    rollups=RollupStore(daily_db)
    while rollups.has_pending_invalidations():
        assert rollups.drain_invalidations(event_limit=256,day_budget=256)>0
    token=rollups.upsert_building('day',first.isoformat(),complete.session_id)
    fingerprint=hashlib.sha256(complete.summary.encode()).hexdigest()
    # The published content is a verbatim source extract. This exercises the
    # real rollup commit/certificate protocol without fabricating a model result.
    assert rollups.mark_ready(token,complete.summary,count_tokens(complete.summary),[complete.node_id],fingerprint,time_basis='observed_at_v1')
    assert rollups.get_rollup('day',first.isoformat(),complete.session_id)['time_basis']=='observed_at_v1'
    with daily_dag.connection:
        daily_dag.connection.execute('UPDATE summary_nodes SET summary=summary WHERE node_id=?',(complete.node_id,))
    assert rollups.has_pending_invalidations(complete.session_id)
    event=rollups.connection.execute('SELECT 1 FROM lcm_rollup_invalidations WHERE scope=? AND covered_start=? AND covered_end=?',(complete.session_id,complete.event_time.earliest_at,complete.event_time.latest_at)).fetchone()
    assert event is not None
    rollups.drain_invalidations(event_limit=256,day_budget=256)
    assert rollups.get_rollup('day',first.isoformat(),complete.session_id)['status']=='stale'
    checks['event_rollup_certificate_and_invalidation']=True
    rollups.close()
    daily_dag.close()
    assert not network_attempts, 'Offline acceptance attempted a network connection'
    receipt['network_connect_attempts']=len(network_attempts)
    with (out/'receipt.json').open('w') as stream:
        os.fchmod(stream.fileno(),0o600)
        json.dump(receipt,stream,indent=2)
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':
    main()
