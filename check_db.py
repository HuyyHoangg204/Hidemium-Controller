import pymongo, json

client = pymongo.MongoClient('mongodb+srv://phamvanlong11032000_db_user:VV4wI66EdtyozVux@cluster0.obbfrbk.mongodb.net/')
db = client['veo_db']

print('=== HIEN TRANG DB ===')
for col in db.list_collection_names():
    cnt = db[col].count_documents({})
    print(f'  {col}: {cnt} docs')

print('\n=== veo_accounts CON LAI (25 acc) ===')
for d in db['veo_accounts'].find({}, {'_id': 0, 'name': 1, 'is_active': 1}).sort('name', 1):
    print(f"  {d['name']} | is_active={d.get('is_active', '?')}")

print('\n=== deleted_accounts (acc da xoa) ===')
for d in db['deleted_accounts'].find({}, {'_id': 0, 'name': 1, 'delete_reason': 1, 'deleted_at': 1}).sort('name', 1):
    reason = d.get('delete_reason', 'khong ro')
    print(f"  {d['name']} | {reason}")

print('\n=== KIEM TRA: video_tasks tham chieu acc da xoa? ===')
# Lay danh sach ten acc da xoa
deleted_names = [d['name'] for d in db['deleted_accounts'].find({}, {'name': 1})]
# Kiem tra trong video_tasks
affected_tasks = db['video_tasks'].count_documents({'account_name': {'$in': deleted_names}})
print(f'  video_tasks tham chieu den acc da xoa: {affected_tasks} tasks')

# Kiem tra trong user_job_results
affected_jobs = db['user_job_results'].count_documents({'account_name': {'$in': deleted_names}})
print(f'  user_job_results tham chieu den acc da xoa: {affected_jobs} records')

client.close()
