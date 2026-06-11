import re
from typing import Callable, List

import numpy as np
from lightrag.utils import logger, Tokenizer


class SemanticSplitter:
    def __init__(
        self,
        embedding_func: Callable,
        tokenizer: Tokenizer | None = None,
        buffer_size: int = 1,
        breakpoint_percentile_threshold: int = 95,
        min_chunk_size: int = 100,
        max_token_size: int = 1000,
        embedding_batch_size: int = 100,
    ):
        self.embedding_func = embedding_func
        self.tokenizer = tokenizer
        self.buffer_size = buffer_size
        self.breakpoint_percentile_threshold = breakpoint_percentile_threshold
        self.min_chunk_size = min_chunk_size
        self.max_token_size = max_token_size
        self.embedding_batch_size = embedding_batch_size

    async def split_text(self, text: str) -> List[str]:
        # 1. Split into sentences
        # Simple sentence splitting using regex
        sentences = re.split(r"(?<=[.?!。？！])\s+", text)
        sentences = [s.strip() for s in sentences if s.strip()]

        if not sentences:
            return []

        if len(sentences) == 1:
            return self._ensure_token_limit([text])

        # 2. Combine sentences with buffer (sliding window) for context
        combined_sentences = []
        for i in range(len(sentences)):
            combined_sentence = ""
            # Add previous sentences
            for j in range(i - self.buffer_size, i):
                if j >= 0:
                    combined_sentence += sentences[j] + " "

            combined_sentence += sentences[i]

            # Add next sentences
            for j in range(i + 1, i + 1 + self.buffer_size):
                if j < len(sentences):
                    combined_sentence += " " + sentences[j]

            combined_sentences.append(combined_sentence)

        # 3. Calculate embeddings with batch processing
        try:
            embeddings = await self._batch_calculate_embeddings(combined_sentences)
            # Ensure embeddings is a numpy array
            if not isinstance(embeddings, np.ndarray):
                embeddings = np.array(embeddings)
        except Exception as e:
            logger.error(f"Error calculating embeddings for semantic splitting: {e}")
            # Fallback to returning original sentences as one chunk or simple split
            return self._ensure_token_limit([text])

        # 4. Calculate cosine distances between adjacent sentences using vectorized operations
        distances = self._calculate_cosine_distances_vectorized(embeddings)

        # 5. Determine breakpoints
        # Using percentile to determine threshold
        if distances is None or (isinstance(distances, np.ndarray) and distances.size == 0):
            return self._ensure_token_limit([text])

        breakpoint_distance_threshold = np.percentile(
            distances, self.breakpoint_percentile_threshold
        )

        indices_above_thresh = [
            i for i, x in enumerate(distances) if x > breakpoint_distance_threshold
        ]

        # 6. Group sentences into chunks
        chunks = []
        start_index = 0

        for index in indices_above_thresh:
            end_index = index + 1

            group = sentences[start_index:end_index]
            chunk_text = " ".join(group)
            chunks.append(chunk_text)

            start_index = end_index

        # Add the last chunk
        if start_index < len(sentences):
            group = sentences[start_index:]
            chunk_text = " ".join(group)
            chunks.append(chunk_text)

        # 7. Merge chunks that are too short
        chunks = self._merge_short_chunks(chunks)

        # 8. Ensure all chunks respect token limit
        chunks = self._ensure_token_limit(chunks)

        return chunks

    def _ensure_token_limit(self, chunks: List[str]) -> List[str]:
        """
        Ensure all chunks respect the max token size limit.
        If a chunk exceeds the limit, split it further.

        Args:
            chunks: List of text chunks

        Returns:
            List of chunks that respect token limit
        """
        if not self.tokenizer:
            # No tokenizer available, return as-is
            return chunks

        result = []
        for chunk in chunks:
            tokens = self.tokenizer.encode(chunk)
            if len(tokens) <= self.max_token_size:
                result.append(chunk)
            else:
                # Split chunk by token size
                for start in range(0, len(tokens), self.max_token_size):
                    end = min(start + self.max_token_size, len(tokens))
                    chunk_tokens = tokens[start:end]
                    chunk_text = self.tokenizer.decode(chunk_tokens)
                    result.append(chunk_text)

        return result

    async def _batch_calculate_embeddings(self, sentences: List[str]) -> np.ndarray:
        """
        Batch calculate embeddings for sentences to optimize performance.

        Args:
            sentences: List of sentences to embed

        Returns:
            numpy array of embeddings
        """
        if len(sentences) <= self.embedding_batch_size:
            # If total sentences is less than batch size, process all at once
            return await self.embedding_func(sentences)

        # Process in batches
        all_embeddings = []
        for i in range(0, len(sentences), self.embedding_batch_size):
            batch = sentences[i : i + self.embedding_batch_size]
            batch_embeddings = await self.embedding_func(batch)
            if isinstance(batch_embeddings, np.ndarray):
                all_embeddings.append(batch_embeddings)
            else:
                all_embeddings.append(np.array(batch_embeddings))

        # Concatenate all batch embeddings
        return np.vstack(all_embeddings)

    def _calculate_cosine_distances_vectorized(
        self, embeddings: np.ndarray
    ) -> np.ndarray:
        """
        Calculate cosine distances between adjacent embeddings using vectorized operations.

        Args:
            embeddings: numpy array of shape (n, embedding_dim)

        Returns:
            numpy array of distances between adjacent embeddings
        """
        if len(embeddings) < 2:
            return np.array([])

        # Normalize embeddings (L2 norm)
        # Shape: (n, embedding_dim)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        # Avoid division by zero
        norms = np.where(norms == 0, 1, norms)
        normalized_embeddings = embeddings / norms

        # Calculate cosine similarities between adjacent embeddings
        # similarity[i] = dot(embedding[i], embedding[i+1])
        # Shape: (n-1,)
        similarities = np.sum(
            normalized_embeddings[:-1] * normalized_embeddings[1:], axis=1
        )

        # Clip similarities to [-1, 1] to avoid numerical errors
        similarities = np.clip(similarities, -1.0, 1.0)

        # Convert to distances
        distances = 1 - similarities

        return distances

    def _merge_short_chunks(self, chunks: List[str]) -> List[str]:
        """
        Merge chunks that are too short with adjacent chunks.

        Args:
            chunks: List of text chunks

        Returns:
            List of merged chunks with minimum length constraint
        """
        if not chunks:
            return []

        if len(chunks) == 1:
            return chunks

        merged_chunks = []
        i = 0

        while i < len(chunks):
            current_chunk = chunks[i]

            if len(current_chunk) >= self.min_chunk_size:
                merged_chunks.append(current_chunk)
                i += 1
            else:
                if i < len(chunks) - 1:
                    next_chunk = chunks[i + 1]
                    merged_chunk = current_chunk + " " + next_chunk
                    merged_chunks.append(merged_chunk)
                    i += 2
                elif i > 0 and merged_chunks:
                    merged_chunks[-1] = merged_chunks[-1] + " " + current_chunk
                    i += 1
                else:
                    merged_chunks.append(current_chunk)
                    i += 1

        return merged_chunks
