import json, pymongo, time

# Doc ket qua test moi nhat
with open('cookie_test_results.json', encoding='utf-8') as f:
    data = json.load(f)

failed = [r for r in data['results'] if not r['ok']]
print(f'Tong so acc loi: {len(failed)}')
print()
for r in failed:
    print(f"  - {r['account_name']} | stage={r['stage']} | err={r.get('error','-')}")

print(f'\n{"="*55}')
print('Dang xoa khoi veo_accounts va chuyen sang deleted_accounts...')

client = pymongo.MongoClient('mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/')
db = client['veo_db']

deleted_count = 0
not_found = []

for r in failed:
    name = r['account_name']
    doc = db['veo_accounts'].find_one({'name': name})
    if not doc:
        not_found.append(name)
        continue
    # Soft delete: chuyen sang deleted_accounts
    doc.pop('_id', None)   # xoa _id de tranh conflict khi upsert
    doc['deleted_at'] = time.time()
    doc['delete_reason'] = f"cookie_fail: {r.get('error', r.get('stage', '-'))}"
    db['deleted_accounts'].replace_one({'name': name}, doc, upsert=True)
    db['veo_accounts'].delete_one({'name': name})
    print(f'  [XOA] {name}')
    deleted_count += 1

print(f'\nKet qua:')
print(f'  Da xoa: {deleted_count} acc')
print(f'  Khong tim thay: {len(not_found)} acc')
if not_found:
    for n in not_found:
        print(f'    - {n}')

remaining = db['veo_accounts'].count_documents({})
print(f'\n  Con lai trong veo_accounts: {remaining} acc')
client.close()
