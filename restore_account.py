import pymongo, json

client = pymongo.MongoClient('mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/')
db = client['veo_db']

email = 'phamvanlong11032000@gmail.com'

# Lay du lieu tu deleted_accounts
doc = db['deleted_accounts'].find_one({'name': email})
if not doc:
    print('KHONG TIM THAY trong deleted_accounts!')
    client.close()
    exit(1)

print('Tim thay trong deleted_accounts:')
print(json.dumps(doc, ensure_ascii=False, default=str))

# Khoi phuc: insert vao veo_accounts
doc['is_active'] = True  # bat lai
result = db['veo_accounts'].insert_one(doc)
print(f'\nDa khoi phuc vao veo_accounts! inserted_id={result.inserted_id}')

# Xoa khoi deleted_accounts
db['deleted_accounts'].delete_one({'name': email})
print(f'Da xoa khoi deleted_accounts.')

# Xac nhan
check = db['veo_accounts'].find_one({'name': email}, {'_id': 0, 'name': 1, 'is_active': 1, 'cookie': 1})
if check:
    ck = check.get('cookie','')
    cklen = len(ck) if isinstance(ck, str) else len(str(ck))
    print(f'\nXac nhan: {check["name"]} | is_active={check["is_active"]} | cookie_len={cklen}')
else:
    print('CANH BAO: Khong tim thay sau khi khoi phuc!')

client.close()
