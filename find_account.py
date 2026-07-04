import pymongo, json

client = pymongo.MongoClient('mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/')
db = client['veo_db']

print('=== deleted_accounts ===')
deleted = list(db['deleted_accounts'].find({}, {'_id': 0, 'name': 1, 'email': 1, 'is_active': 1}))
for d in deleted:
    print(d)

print('\n=== Tim phamvanlong trong moi collection ===')
for col in db.list_collection_names():
    try:
        docs = list(db[col].find(
            {'$or': [
                {'name': {'$regex': 'phamvanlong', '$options': 'i'}},
                {'email': {'$regex': 'phamvanlong', '$options': 'i'}}
            ]},
            {'_id': 0, 'name': 1, 'email': 1}
        ).limit(5))
        if docs:
            print(f'  [{col}]:', docs)
    except Exception as e:
        pass

print('\n=== 10 account dau trong veo_accounts ===')
for d in db['veo_accounts'].find({}, {'_id': 0, 'name': 1, 'is_active': 1}).sort('name', 1).limit(10):
    print(d)

client.close()
