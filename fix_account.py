# pyrefly: ignore [missing-import]
import pymongo, uuid, time, json

client = pymongo.MongoClient('mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/')
db = client['veo_db']

# Kiem tra phamvanlong trong veo_accounts
doc = db['veo_accounts'].find_one({'name': 'phamvanlong11032000@gmail.com'})
print('Current doc:')
print(json.dumps({k: v for k, v in doc.items() if k != '_id'}, ensure_ascii=False, default=str))

# Kiem tra xem co thieu id va cookie k
has_id = bool(doc.get('id'))
has_cookie = bool(doc.get('cookie'))
print(f'\nhas_id: {has_id} | has_cookie: {has_cookie}')

if not has_id or not has_cookie:
    print('\n=> DANG FIX: Them id va cookie de web app khong bi loi...')
    update = {}
    if not has_id:
        update['id'] = str(uuid.uuid4())
        print(f"  + Them id: {update['id']}")
    if not has_cookie:
        update['cookie'] = ''  # Empty cookie — se duoc cap nhat khi login lai
        print(f"  + Them cookie: (trong)")
    # Xoa deleted_at neu co (khong phai field cua VeoAccount)
    update['is_active'] = True
    db['veo_accounts'].update_one(
        {'name': 'phamvanlong11032000@gmail.com'},
        {'$set': update, '$unset': {'deleted_at': '', 'delete_reason': ''}}
    )
    print('\nFix xong! Kiem tra lai:')
    doc2 = db['veo_accounts'].find_one({'name': 'phamvanlong11032000@gmail.com'})
    print(json.dumps({k: v for k, v in doc2.items() if k != '_id'}, ensure_ascii=False, default=str))
else:
    print('\nDoc da du field, khong can fix.')

# Kiem tra tat ca acc con lai xem co bi thieu id khong
print('\n=== Kiem tra tat ca acc trong veo_accounts ===')
bad = list(db['veo_accounts'].find({'$or': [{'id': {'$exists': False}}, {'id': None}, {'cookie': {'$exists': False}}]}, {'name': 1, 'id': 1}))
if bad:
    print(f'Co {len(bad)} acc bi thieu id/cookie:')
    for b in bad:
        print(f'  - {b.get("name")} | id={b.get("id")}')
else:
    print('Tat ca {0} acc deu co id va cookie OK.'.format(db['veo_accounts'].count_documents({})))

client.close()
