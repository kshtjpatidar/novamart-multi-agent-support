#!/usr/bin/env python3
"""
setup_knowledge_bases.py
========================
Automated setup and sync for the three Bedrock Knowledge Bases:
  1. Returns Policy   -> policies/returns/   -> returns-policy-index
  2. Shipping Policy  -> policies/shipping/  -> shipping-policy-index
  3. Warranty Policy  -> policies/warranty/  -> warranty-policy-index

Uses S3 Vectors backing store and Amazon Titan Embed Text v2 embedding model.
"""

import boto3
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

bedrock_agent = boto3.client('bedrock-agent', region_name=config.AWS_REGION)

KB_SPECS = [
    {
        'domain': 'returns',
        'env_key': 'RETURNS_KB_ID',
        'kb_name': 'novamart-returns-policy-kb',
        'description': 'NovaMart Returns Policy Knowledge Base',
        'index_name': 'returns-policy-index',
        'prefix': 'policies/returns/',
    },
    {
        'domain': 'shipping',
        'env_key': 'SHIPPING_KB_ID',
        'kb_name': 'novamart-shipping-policy-kb',
        'description': 'NovaMart Shipping Policy Knowledge Base',
        'index_name': 'shipping-policy-index',
        'prefix': 'policies/shipping/',
    },
    {
        'domain': 'warranty',
        'env_key': 'WARRANTY_KB_ID',
        'kb_name': 'novamart-warranty-policy-kb',
        'description': 'NovaMart Warranty Policy Knowledge Base',
        'index_name': 'warranty-policy-index',
        'prefix': 'policies/warranty/',
    },
]


def wait_for_kb_active(kb_id: str, timeout: int = 180) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
        status = resp['knowledgeBase']['status']
        if status == 'ACTIVE':
            return status
        if 'FAIL' in status or status == 'DELETING':
            raise RuntimeError(f"KB {kb_id} entered status {status}")
        time.sleep(5)
    raise TimeoutError(f"KB {kb_id} not ACTIVE after {timeout}s")


def get_or_create_kb(spec: dict) -> str:
    # Check existing KBs
    existing = bedrock_agent.list_knowledge_bases(maxResults=50).get('knowledgeBaseSummaries', [])
    for kb in existing:
        if kb['name'] == spec['kb_name']:
            print(f"  Found existing KB: {spec['kb_name']} ({kb['knowledgeBaseId']})")
            wait_for_kb_active(kb['knowledgeBaseId'])
            return kb['knowledgeBaseId']

    print(f"  Creating Knowledge Base: {spec['kb_name']}...")
    resp = bedrock_agent.create_knowledge_base(
        name=spec['kb_name'],
        description=spec['description'],
        roleArn=config.AGENTCORE_ROLE_ARN,
        knowledgeBaseConfiguration={
            'type': 'VECTOR',
            'vectorKnowledgeBaseConfiguration': {
                'embeddingModelArn': f'arn:aws:bedrock:{config.AWS_REGION}::foundation-model/amazon.titan-embed-text-v2:0',
            }
        },
        storageConfiguration={
            'type': 'S3_VECTORS',
            's3VectorsConfiguration': {
                'vectorBucketArn': config.VECTOR_STORE_BUCKET_ARN,
                'indexName': spec['index_name']
            }
        }
    )
    kb_id = resp['knowledgeBase']['knowledgeBaseId']
    print(f"  KB created: {kb_id}. Waiting for ACTIVE status...")
    wait_for_kb_active(kb_id)
    print(f"  KB {kb_id} is ACTIVE.")
    return kb_id


def get_or_create_data_source(kb_id: str, spec: dict) -> str:
    ds_list = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id).get('dataSourceSummaries', [])
    ds_name = f"{spec['domain']}-policy-data-source"
    for ds in ds_list:
        if ds['name'] == ds_name:
            print(f"  Found existing data source: {ds_name} ({ds['dataSourceId']})")
            return ds['dataSourceId']

    print(f"  Creating data source: {ds_name}...")
    resp = bedrock_agent.create_data_source(
        knowledgeBaseId=kb_id,
        name=ds_name,
        description=f"S3 data source for {spec['domain']} policy documents",
        dataSourceConfiguration={
            'type': 'S3',
            's3Configuration': {
                'bucketArn': f"arn:aws:s3:::{config.POLICY_BUCKET}",
                'inclusionPrefixes': [spec['prefix']],
            }
        }
    )
    ds_id = resp['dataSource']['dataSourceId']
    print(f"  Data source created: {ds_id}")
    return ds_id


def sync_data_source(kb_id: str, ds_id: str, timeout: int = 300) -> None:
    # Check if there is already a completed ingestion job
    jobs = bedrock_agent.list_ingestion_jobs(
        knowledgeBaseId=kb_id, dataSourceId=ds_id
    ).get('ingestionJobSummaries', [])
    
    running = [j for j in jobs if j['status'] in ('STARTING', 'IN_PROGRESS')]
    if running:
        job_id = running[0]['ingestionJobId']
        print(f"  Sync already in progress (job: {job_id}). Waiting for completion...")
    else:
        completed = [j for j in jobs if j['status'] == 'COMPLETE']
        if completed:
            print(f"  Ingestion job already completed ({completed[0]['ingestionJobId']}). Re-syncing for latest documents...")
        print("  Starting new ingestion job (Sync)...")
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            description='Automated sync of policy documents'
        )
        job_id = resp['ingestionJob']['ingestionJobId']

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            ingestionJobId=job_id
        )['ingestionJob']
        status = job['status']
        if status == 'COMPLETE':
            docs = job.get('statistics', {}).get('numberOfDocumentsScanned', 0)
            print(f"  ✓ Sync COMPLETE! Documents scanned: {docs}")
            return
        if status in ('FAILED', 'STOPPED'):
            reasons = job.get('failureReasons', [])
            raise RuntimeError(f"Ingestion job failed: {reasons}")
        print('.', end='', flush=True)
        time.sleep(5)
    raise TimeoutError(f"Ingestion job timed out after {timeout}s")


def update_env(kb_ids: dict):
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    with open(env_path, 'r') as f:
        content = f.read()

    import re
    for key, val in kb_ids.items():
        content = re.sub(rf"^{key}=.*$", f"{key}={val}", content, flags=re.MULTILINE)

    with open(env_path, 'w') as f:
        f.write(content)
    print(f"\nUpdated {env_path} with KB IDs:")
    for k, v in kb_ids.items():
        print(f"  {k}={v}")


def main():
    print("="*60)
    print("  Setting up NovaMart Bedrock Knowledge Bases")
    print("="*60)

    kb_ids = {}
    for spec in KB_SPECS:
        print(f"\n[Domain: {spec['domain'].upper()}]")
        kb_id = get_or_create_kb(spec)
        kb_ids[spec['env_key']] = kb_id
        ds_id = get_or_create_data_source(kb_id, spec)
        sync_data_source(kb_id, ds_id)

    update_env(kb_ids)
    print("\n✅ All 3 Knowledge Bases created, synced, and saved to .env!")


if __name__ == '__main__':
    main()
