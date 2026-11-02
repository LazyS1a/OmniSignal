"""Read-only SSH snapshot of the design-rule rollout; never schedules work."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

REMOTE = r'''
import collections,datetime,json,pathlib,sqlite3,subprocess
c=json.loads(subprocess.check_output(['docker','inspect','omnisignal-ad-workbench-1'],timeout=15))[0]
env=dict(x.split('=',1) for x in c['Config']['Env'] if '=' in x)
root=pathlib.Path(next(m['Source'] for m in c['Mounts'] if m['Destination']=='/app/workbench-data'))
assert root.resolve().is_relative_to(pathlib.Path('/opt/omnisignal/data'))
now=datetime.datetime.now(datetime.timezone.utc);day=now.astimezone(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
db=sqlite3.connect('file:'+str(root/'workbench.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('pragma query_only=on');db.execute('BEGIN')
jobs=[dict(r) for r in db.execute("select id,provider,status,charged,created_at,quota_day from analysis_jobs where date(created_at,'+8 hours')=?",(day,))]
new=[j for j in jobs if j['provider'].startswith('design-v1:')]
reviewed=set();published=set();projects=set()
for a in db.execute("select action,entity,details from audit where actor='system:auto-editorial' and date(at,'+8 hours')=?",(day,)):
 if a['action']=='automated_review':reviewed.add(a['entity'])
 elif a['action']=='publish':published.update(json.loads(a['details']).get('assets',[]))
for j in jobs:
 row=db.execute('select document from assets where id=(select asset_id from analysis_jobs where id=?)',(j['id'],)).fetchone()
 if row:
  project=json.loads(row[0]).get('project_id')
  if project:projects.add(project)
quality=collections.Counter();profiles=collections.Counter();versions=collections.Counter()
for j in new:
 row=db.execute('select document from analysis_details where job_id=?',(j['id'],)).fetchone()
 if row:
  d=json.loads(row[0]);quality[d.get('quality',{}).get('status','unknown')]+=1;profiles[d.get('profile','unknown')]+=1;versions[d.get('prompt_version','unknown')]+=1
permissions=0
for r in db.execute("select document from assets where status='accepted'"):
 a=json.loads(r[0])
 if a.get('reviewed_analysis',{}).get('provenance')=='model_automated_review' and any(a.get('permissions',{}).get(k)!='allowed' for k in ['display','export']):permissions+=1
state=db.execute("select value from meta where key='auto_editorial_status'").fetchone();tick=json.loads(state[0]) if state else None
policy=json.loads((root/'operations/auto-editorial-policy.json').read_text())
result={'checked_at':now.isoformat(),'day':day,'timezone':'Asia/Shanghai','runtime_sha':env.get('OMNISIGNAL_SOURCE_COMMIT'),'health':c['State'].get('Health',{}).get('Status'),'worker_enabled':env.get('OMNISIGNAL_WORKBENCH_WORKER')=='true','design_skills_enabled':env.get('OMNISIGNAL_WORKBENCH_DESIGN_SKILLS') in ['on','true','1'],'design_skills_daily_limit':int(env.get('OMNISIGNAL_WORKBENCH_DESIGN_SKILLS_DAILY_LIMIT','0')),'design_skills_quota_timezone':'UTC','auto_publish':policy['auto_publish'],'today_all_job_states':dict(collections.Counter(j['status'] for j in jobs)),'today_new_rules_job_states':dict(collections.Counter(j['status'] for j in new)),'today_new_rules_charged':sum(j['charged'] for j in new),'quality_status':dict(quality),'profiles':dict(profiles),'prompt_versions':dict(versions),'accepted_waiting_usage_basis':permissions,'assets_by_status':{r[0]:r[1] for r in db.execute('select status,count(*) from assets group by status')},'publication_records':db.execute('select count(*) from releases').fetchone()[0],'last_tick':{k:v for k,v in (tick or {}).items() if k in ['at','status','reviewed','published','blocked','blocked_reasons']},'human_reviewed':None,'receiver_receipt':'unconfirmed','cost':None,'read_only':True,'business_submissions':0}
result.update(today_ai_reviewed=len(reviewed),today_automatically_published_assets=len(published),today_project_coverage=len(projects))
result['accepted_original_permissions_unknown']=permissions
result['internal_publication_policy_enabled']=bool(policy.get('publication_authorized'))
has_grants=db.execute("select 1 from sqlite_master where type='table' and name='publication_use_grants'").fetchone()
result['internal_publication_grants']=db.execute('select count(*) from publication_use_grants').fetchone()[0] if has_grants else 0
if result['internal_publication_policy_enabled']:
 # Original per-image permissions remain unchanged by a separate internal-use
 # policy. They cannot be counted as current blocked delivery candidates.
 result['accepted_waiting_usage_basis']=None
result['last_tick_usage_blocked']=(tick or {}).get('blocked_reasons',{}).get('display_export_permission_required')
db.close();print(json.dumps(result))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh', default='D:/Git/usr/bin/ssh.exe')
    parser.add_argument('--ssh-config', default='C:/Users/Administrator/.ssh/omnisignal_hk.conf')
    parser.add_argument('--host', default='omnisignal-cloud')
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--output', type=Path, default=Path('artifacts/private/daily-pilot-reports'))
    args = parser.parse_args()
    try:
        result = subprocess.run([args.ssh, '-F', args.ssh_config, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ConnectionAttempts=1', args.host, 'sudo -n python3 -'], input=REMOTE.encode(), capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError('read_only_snapshot_unavailable')
        value = json.loads(result.stdout)
        value['expected_runtime'] = value['runtime_sha'] == args.expected_sha
        args.output.mkdir(parents=True, exist_ok=True)
        stem = value['day'] + '-design-skills'
        (args.output / (stem + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        (args.output / (stem + '.md')).write_text(
            f"# Omni 专项规则运行快照 · {value['day']}\n\n"
            f"服务：{value['health']}；版本符合预期：{value['expected_runtime']}；自动发布：{value['auto_publish']}。\n\n"
            f"当日全部任务：{value['today_all_job_states']}。新版任务：{value['today_new_rules_job_states']}。\n\n"
            f"内容检查：{value['quality_status']}；原逐图用途记录未齐：{value['accepted_original_permissions_unknown']}；内部策略已启用：{value['internal_publication_policy_enabled']}；已记录内部交付依据：{value['internal_publication_grants']}。\n\n"
            f"最近一轮因用途阻断：{value['last_tick_usage_blocked']}（空值表示未记录，不能当成0）。\n\n"
            "人工完成量、实际成本未知；下游接收回执未确认。本次只有读取，没有业务提交。\n", encoding='utf-8')
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'unavailable', 'error_type': type(error).__name__, 'read_only': True, 'business_submissions': 0}))
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
