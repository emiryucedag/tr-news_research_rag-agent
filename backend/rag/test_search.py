import chromadb
from sentence_transformers import SentenceTransformer

CHROMA_DIR = "rag/chroma_db"
COLLECTION_NAME = "news"
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"


def search(query, n_results=5):
    model = SentenceTransformer(MODEL_NAME)
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = client.get_collection(COLLECTION_NAME)

    query_embedding = model.encode([query]).tolist() #transofrms the query into a vector embedding using the multilingual model

    results = collection.query(
        query_embeddings=query_embedding, 
        n_results=n_results,
    ) #to get most relevant matches

    print(f"\nSorgu: '{query}'\n")
    for i in range(len(results["ids"][0])):
        doc = results["documents"][0][i]
        meta = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        print(f"[{i+1}] (mesafe: {distance:.4f}) Kaynak: {meta['source']}")
        print(f"    {doc[:150]}")
        print(f"    {meta['link']}\n")


if __name__ == "__main__":
    # query what you want to search for in the news articles
    search("spor ile ilgili haberler")