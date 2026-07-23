import os
import tempfile
import traceback
from pathlib import Path

p = Path(tempfile.mkdtemp(prefix="chroma_test_"))
print("path", p, "writable", os.access(p, os.W_OK))
import chromadb
from chromadb.config import Settings

client = chromadb.PersistentClient(
    path=str(p),
    settings=Settings(anonymized_telemetry=False, allow_reset=True),
)
col = client.get_or_create_collection(name="docs", metadata={"hnsw:space": "cosine"})
print("count", col.count())
try:
    col.add(
        ids=["1"],
        documents=["hello world"],
        embeddings=[[0.1] * 8],
        metadatas=[{"source": "t"}],
    )
    print("add ok", col.count())
except Exception as e:
    traceback.print_exc()
    print("FAIL", type(e), e)
print("files", list(p.rglob("*"))[:30])
