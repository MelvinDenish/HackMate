"""
╔══════════════════════════════════════════════════════════════╗
║  KNOWLEDGE BASE — ChromaDB RAG Vector Store                  ║
║                                                              ║
║  🔵 Embeddings: Google text-embedding-004                    ║
║  Why: High-quality embeddings, fast, cost-effective          ║
║                                                              ║
║  Purpose:                                                    ║
║  Stores research findings in a vector DB so downstream       ║
║  agents can retrieve grounded, factual information via       ║
║  similarity search (RAG) rather than relying on LLM memory. ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
from pathlib import Path
from typing import Optional

import chromadb

from agents.llm_factory import create_embeddings
from config import PipelineConfig

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    Project-specific vector knowledge base using ChromaDB.

    Ingests research dossiers and other documents, enabling
    downstream agents to query for relevant context via
    semantic similarity search.
    """

    def __init__(self, config: PipelineConfig, collection_name: str = "hackathon_kb"):
        self.config = config
        self.collection_name = collection_name

        # Initialize ChromaDB (persistent, stored in workspace)
        persist_dir = str(
            Path(config.workspace.root) / ".chromadb"
        )
        try:
            # ChromaDB >= 0.5: use PersistentClient
            self._client = chromadb.PersistentClient(
                path=persist_dir,
            )
        except AttributeError:
            # Fallback for older chromadb versions
            self._client = chromadb.Client()

        # Create or get collection
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"description": "Hackathon project knowledge base"},
        )

        # Initialize embeddings model
        embed_spec = config.get_model("embeddings")
        self._embeddings = create_embeddings(embed_spec, config.keys)

        logger.info(
            f"[KB] Initialized collection '{collection_name}' "
            f"with {self._collection.count()} existing documents"
        )

    def _get_embedding(self, text: str) -> list[float]:
        """Generate embedding for a text using Google's model."""
        return self._embeddings.embed_query(text)

    def _get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        return self._embeddings.embed_documents(texts)

    def ingest_document(
        self,
        content: str,
        source: str,
        doc_type: str = "research",
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> int:
        """
        Ingest a document into the knowledge base.

        Uses markdown-aware chunking that splits at heading boundaries,
        preserving section integrity for better RAG retrieval.
        Falls back to character-count chunking for non-markdown content.

        Args:
            content: Full document text
            source: Source identifier (e.g. file path, URL)
            doc_type: Document type for filtering
            chunk_size: Characters per chunk (used for fallback/large sections)
            chunk_overlap: Overlap between chunks (used for fallback)

        Returns:
            Number of chunks ingested
        """
        import re

        # Try markdown-aware chunking first
        chunks = self._chunk_markdown(content, chunk_size)

        # Fallback to character-count chunking if markdown chunking yields nothing
        if not chunks:
            for i in range(0, len(content), chunk_size - chunk_overlap):
                chunk = content[i:i + chunk_size]
                if chunk.strip():
                    chunks.append(chunk)

        if not chunks:
            logger.warning(f"[KB] No chunks to ingest from {source}")
            return 0

        # Generate embeddings
        embeddings = self._get_embeddings_batch(chunks)

        # Create IDs and metadata
        ids = [f"{source}_{i}" for i in range(len(chunks))]
        metadatas = [
            {"source": source, "doc_type": doc_type, "chunk_index": i}
            for i in range(len(chunks))
        ]

        # Upsert to ChromaDB
        self._collection.upsert(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas,
        )

        logger.info(f"[KB] Ingested {len(chunks)} chunks from {source}")
        return len(chunks)

    @staticmethod
    def _chunk_markdown(content: str, max_chunk_size: int = 1500) -> list[str]:
        """Split markdown content at heading boundaries.
        Preserves section integrity for better retrieval quality."""
        import re

        # Split at markdown headings (##, ###, etc.)
        sections = re.split(r'(?=^#{1,3}\s)', content, flags=re.MULTILINE)
        chunks = []
        current_chunk = ""

        for section in sections:
            section = section.strip()
            if not section:
                continue

            # If adding this section exceeds max size, flush current chunk
            if current_chunk and len(current_chunk) + len(section) > max_chunk_size:
                chunks.append(current_chunk.strip())
                current_chunk = section
            else:
                current_chunk += "\n\n" + section if current_chunk else section

        # Don't forget the last chunk
        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        # If no heading-based splits happened, return empty (will use fallback)
        if len(chunks) <= 1 and len(content) > max_chunk_size:
            return []

        return chunks

    def ingest_file(self, file_path: str, doc_type: str = "research") -> int:
        """Ingest a file from the workspace into the knowledge base."""
        path = Path(file_path)
        if not path.exists():
            logger.error(f"[KB] File not found: {file_path}")
            return 0

        content = path.read_text(encoding="utf-8")
        return self.ingest_document(content, source=str(path), doc_type=doc_type)

    def query(
        self,
        question: str,
        n_results: int = 5,
        doc_type: Optional[str] = None,
    ) -> list[dict]:
        """
        Query the knowledge base with a natural language question.

        Args:
            question: The search query
            n_results: Number of results to return
            doc_type: Optional filter by document type

        Returns:
            List of dicts with: content, source, score, metadata
        """
        query_embedding = self._get_embedding(question)

        where_filter = None
        if doc_type:
            where_filter = {"doc_type": doc_type}

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where_filter,
        )

        formatted = []
        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                formatted.append({
                    "content": doc,
                    "source": (results["metadatas"][0][i]["source"]
                               if results["metadatas"] else "unknown"),
                    "score": (results["distances"][0][i]
                              if results["distances"] else 0.0),
                    "metadata": (results["metadatas"][0][i]
                                 if results["metadatas"] else {}),
                })

        logger.info(f"[KB] Query returned {len(formatted)} results")
        return formatted

    def get_context_for_prompt(
        self,
        question: str,
        n_results: int = 5,
        max_chars: int = 4000,
    ) -> str:
        """
        Get formatted context string suitable for insertion
        into an LLM prompt (RAG pattern).
        """
        results = self.query(question, n_results)

        if not results:
            return "(No relevant context found in knowledge base)"

        context_parts = []
        total_chars = 0
        for r in results:
            chunk = f"[Source: {r['source']}]\n{r['content']}\n"
            if total_chars + len(chunk) > max_chars:
                break
            context_parts.append(chunk)
            total_chars += len(chunk)

        return "\n---\n".join(context_parts)

    @property
    def document_count(self) -> int:
        return self._collection.count()
