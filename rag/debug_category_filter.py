import time
import chromadb

CHROMA_DIR = "rag/chroma_db"
COLLECTION_NAME = "news"

client = chromadb.PersistentClient(path=CHROMA_DIR)
collection = client.get_collection(COLLECTION_NAME)

sources = ["Hurriyet Spor", "Sabah Spor", "CNN Turk Spor"]
now_ts = int(time.time())
cutoff_ts = now_ts - 72 * 3600

print(f"Collection total item count: {collection.count()}\n")

# Test 1: filter by source only
r1 = collection.get(where={"source": {"$in": sources}})
print(f"Test 1 - source $in filter only: {len(r1['ids'])} result(s)")

# Test 2: filter by published_ts only
r2 = collection.get(where={"published_ts": {"$gte": cutoff_ts}})
print(f"Test 2 - published_ts $gte filter only: {len(r2['ids'])} result(s)")

# Test 3: both combined with $and
r3 = collection.get(where={"$and": [{"source": {"$in": sources}}, {"published_ts": {"$gte": cutoff_ts}}]})
print(f"Test 3 - combined $and filter: {len(r3['ids'])} result(s)")

# Peek at a sample metadata entry from test 1, to check the actual stored type/value of published_ts
if r1["ids"]:
    print("\nSample metadata from Test 1 (first item):")
    print(r1["metadatas"][0])