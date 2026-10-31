"""attendance/ml_matcher.py — BERT-based semantic name matching.

Implementation:
  - Use sentence-transformers library with multilingual model
  - Generate embeddings for roster names offline (cached)
  - Generate embedding for OCR result at runtime
  - Cosine similarity for matching
  - Threshold tuning based on existing match confidence levels

Training from learned_aliases.json:
  - Extract positive pairs from learned_aliases.json
  - Fine-tune the model on course-specific name variations
  - Periodic retraining pipeline (weekly or on-demand)
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from attendance.cache import get_embedding, set_embedding


class MLMatcher:
    """BERT-based semantic name matcher using sentence-transformers."""

    def __init__(
        self,
        model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
        cache_embeddings: bool = True,
        threshold: float = 0.75,
    ):
        """Initialize the ML matcher.

        Args:
            model_name: Name of the sentence-transformers model to use
            cache_embeddings: Whether to cache embeddings
            threshold: Cosine similarity threshold for accepting matches
        """
        self.model_name = model_name
        self.cache_embeddings = cache_embeddings
        self.threshold = threshold
        self._model = None

    def _load_model(self):
        """Lazy-load the sentence-transformers model."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
            except ImportError:
                raise ImportError(
                    "sentence-transformers is not installed. "
                    "Install it with: pip install sentence-transformers"
                )

    def get_embedding(self, name: str) -> np.ndarray:
        """Get embedding for a name, using cache if enabled.

        Args:
            name: Name to embed

        Returns:
            Embedding vector as numpy array
        """
        # Check cache first
        if self.cache_embeddings:
            cached = get_embedding(name)
            if cached is not None:
                return cached

        # Generate embedding
        self._load_model()
        embedding = self._model.encode(name, convert_to_numpy=True)

        # Cache the result
        if self.cache_embeddings:
            set_embedding(name, embedding)

        return embedding

    def match(
        self,
        ocr_name: str,
        roster_names: list[str],
        threshold: Optional[float] = None,
    ) -> tuple[Optional[str], float, list[tuple[str, float]]]:
        """Find the best matching roster name for an OCR result.

        Args:
            ocr_name: Name extracted from OCR
            roster_names: List of roster names to match against
            threshold: Similarity threshold (uses instance default if None)

        Returns:
            Tuple of (matched_name, similarity_score, top3_candidates) or (None, 0.0, []) if no match
            top3_candidates is a list of (name, score) tuples sorted by score descending
        """
        if not roster_names:
            return None, 0.0, []

        threshold = threshold if threshold is not None else self.threshold

        # Get embedding for OCR name
        ocr_embedding = self.get_embedding(ocr_name)

        # Get embeddings for all roster names
        roster_embeddings = [self.get_embedding(name) for name in roster_names]

        # Calculate cosine similarities
        similarities = cosine_similarity([ocr_embedding], roster_embeddings)[0]

        # Sort by similarity descending and get top 3
        sorted_indices = np.argsort(similarities)[::-1]
        top3 = [(roster_names[i], float(similarities[i])) for i in sorted_indices[:3]]

        # Find best match
        best_idx = int(sorted_indices[0])
        best_score = float(similarities[best_idx])

        if best_score >= threshold:
            return roster_names[best_idx], best_score, top3

        return None, best_score, top3

    def batch_match(
        self,
        ocr_names: list[str],
        roster_names: list[str],
        threshold: Optional[float] = None,
    ) -> list[tuple[Optional[str], float]]:
        """Batch match multiple OCR names against a roster.

        Args:
            ocr_names: List of OCR names to match
            roster_names: List of roster names to match against
            threshold: Similarity threshold

        Returns:
            List of (matched_name, similarity_score) tuples
        """
        if not roster_names:
            return [(None, 0.0) for _ in ocr_names]

        threshold = threshold if threshold is not None else self.threshold

        # Get embeddings for all OCR names
        ocr_embeddings = [self.get_embedding(name) for name in ocr_names]

        # Get embeddings for all roster names
        roster_embeddings = [self.get_embedding(name) for name in roster_names]

        # Calculate cosine similarities for all pairs
        similarities = cosine_similarity(ocr_embeddings, roster_embeddings)

        # Find best match for each OCR name
        results = []
        for i, sim_row in enumerate(similarities):
            best_idx = int(np.argmax(sim_row))
            best_score = float(sim_row[best_idx])
            if best_score >= threshold:
                results.append((roster_names[best_idx], best_score))
            else:
                results.append((None, best_score))

        return results

    def warm_cache(self, names: list[str]) -> None:
        """Warm the embedding cache by pre-computing embeddings.

        Args:
            names: List of names to embed and cache
        """
        for name in names:
            self.get_embedding(name)


# ─── Training from learned_aliases.json ─────────────────────────────────────────

def train_from_aliases(
    aliases_path: str | Path,
    model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
    output_path: Optional[str | Path] = None,
    epochs: int = 3,
    batch_size: int = 16,
) -> None:
    """Fine-tune the model on learned aliases.

    Args:
        aliases_path: Path to learned_aliases.json
        model_name: Base model name to fine-tune
        output_path: Path to save the fine-tuned model (defaults to aliases_{model_name})
        epochs: Number of training epochs
        batch_size: Training batch size
    """
    try:
        from sentence_transformers import SentenceTransformer, InputExample, losses, util
    except ImportError:
        raise ImportError(
            "sentence-transformers is not installed. "
            "Install it with: pip install sentence-transformers"
        )

    # Load learned aliases
    aliases_path = Path(aliases_path)
    with open(aliases_path, "r", encoding="utf-8") as f:
        aliases_data = json.load(f)

    # Extract positive pairs (raw_name -> roster_name)
    examples = []
    for course_key, course_aliases in aliases_data.items():
        for raw_name, roster_name in course_aliases.items():
            examples.append(InputExample(texts=[raw_name, roster_name], label=1.0))

    if not examples:
        print("No alias pairs found for training.")
        return

    # Load base model
    model = SentenceTransformer(model_name)

    # Create dataloader
    from torch.utils.data import DataLoader
    train_dataloader = DataLoader(examples, shuffle=True, batch_size=batch_size)

    # Use MultipleNegativesRankingLoss for fine-tuning (treats other batch elements as negatives)
    train_loss = losses.MultipleNegativesRankingLoss(model=model)

    # Fine-tune
    model.fit(
        train_objectives=[(train_dataloader, train_loss)],
        epochs=epochs,
        warmup_steps=10,
        show_progress_bar=True,
    )

    # Save fine-tuned model
    if output_path is None:
        output_path = f"aliases_{model_name.replace('/', '_')}"
    model.save(str(output_path))
    print(f"Fine-tuned model saved to {output_path}")


# ─── Global ML matcher instance ────────────────────────────────────────────────

_ml_matcher: Optional[MLMatcher] = None
_ml_matcher_lock = threading.Lock()


def init_ml_matcher(
    model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
    cache_embeddings: bool = True,
    threshold: float = 0.75,
) -> MLMatcher:
    """Initialize the global ML matcher.

    Args:
        model_name: Name of the sentence-transformers model
        cache_embeddings: Whether to cache embeddings
        threshold: Similarity threshold

    Returns:
        The ML matcher instance
    """
    global _ml_matcher
    if _ml_matcher is None:
        with _ml_matcher_lock:
            if _ml_matcher is None:
                _ml_matcher = MLMatcher(model_name, cache_embeddings, threshold)
    return _ml_matcher


def get_ml_matcher() -> Optional[MLMatcher]:
    """Get the global ML matcher instance."""
    return _ml_matcher
