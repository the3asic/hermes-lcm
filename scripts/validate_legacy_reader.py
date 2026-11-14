#!/usr/bin/env python3
"""Prove a prior source snapshot can reopen an offline candidate database."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--core',type=Path,required=True)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--node-id',type=int,required=True)
    args=parser.parse_args()
    source=args.source.resolve(strict=True)
    sys.path.insert(0,str(args.core.resolve(strict=True)))
    spec=importlib.util.spec_from_file_location('hermes_lcm',source/'__init__.py',submodule_search_locations=[str(source)])
    sys.modules['hermes_lcm']=importlib.util.module_from_spec(spec)
    from hermes_lcm.dag import SummaryDAG
    from hermes_lcm.store import MessageStore
    dag=SummaryDAG(args.database)
    node=dag.get_node(args.node_id)
    assert node is not None and node.summary and node.source_ids
    store=MessageStore(args.database)
    assert store.get_session_messages(node.session_id)
    dag.close()
    store.close()
    print(json.dumps({'prior_source':str(source),'database':str(args.database),'node_id':node.node_id,'reopened':True}))


if __name__=='__main__':
    main()
