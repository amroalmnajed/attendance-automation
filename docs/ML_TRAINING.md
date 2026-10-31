# ML Matching Training Guide

## Overview

The attendance automation system uses sentence-transformers for semantic name matching. This guide covers model selection, training, fine-tuning, and evaluation.

## Model Selection

### Default Model
- **Model**: `paraphrase-multilingual-MiniLM-L12-v2`
- **Why**: Multilingual support for Arabic names, small size (420MB), good performance on short text
- **Alternative Models**:
  - `paraphrase-multilingual-mpnet-base-v2` (larger, better performance)
  - `all-MiniLM-L6-v2` (English-only, faster)
  - Custom fine-tuned model (recommended for production)

### Model Requirements
- Must support Arabic script (for Arabic names)
- Must work well with short text (names are typically 2-3 words)
- Should have inference latency < 100ms per name
- Should be small enough for local deployment

## Training Pipeline

### 1. Initial Setup

```python
from attendance.ml_matcher import MLMatcher

# Initialize matcher with default model
matcher = MLMatcher(
    model_name="paraphrase-multilingual-MiniLM-L12-v2",
    cache_embeddings=True,
    threshold=0.75,
)
```

### 2. Data Collection

The system automatically collects training data from two sources:

#### Learned Aliases
- Collected from review decisions when users confirm matches
- Stored in `config/learned_aliases.json` and database `aliases` table
- Format: `{course_key: {raw_name: {roster_name, confirmations, first_seen, last_used}}}`

#### Manual Labeling
- For rare cases where aliases are insufficient
- Add positive and negative examples to training set
- Format: `[(ocr_name, roster_name, is_match), ...]`

### 3. Fine-Tuning

Fine-tuning improves model performance on course-specific name variations.

#### When to Fine-Tune
- After collecting at least 100 confirmed aliases per course
- When baseline accuracy is below 80%
- Weekly (configurable via `ml_matching.retrain_interval_days`)

#### Fine-Tuning Process

```python
from sentence_transformers import SentenceTransformer, InputExample, losses
from torch.utils.data import DataLoader

# Load base model
model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

# Load training data from learned aliases
from attendance.db import SessionLocal, Alias
from attendance.migrate import DataMigrator

session = SessionLocal()
aliases = session.query(Alias).filter_by(course_id=course_id).all()

# Create training examples
train_examples = []
for alias in aliases:
    if alias.confirmations >= 2:  # Only use high-confidence aliases
        train_examples.append(
            InputExample(
                texts=[alias.raw_name_key, alias.roster_name],
                label=1.0  # Positive pair
            )
        )

# Add negative examples (random mismatched pairs)
import random
for _ in range(len(train_examples)):
    ocr_name = random.choice([a.raw_name_key for a in aliases])
    roster_name = random.choice([a.roster_name for a in aliases])
    # Ensure they're not actually a match
    if not any(a.raw_name_key == ocr_name and a.roster_name == roster_name for a in aliases):
        train_examples.append(
            InputExample(
                texts=[ocr_name, roster_name],
                label=0.0  # Negative pair
            )
        )

# Create dataloader
train_dataloader = DataLoader(train_examples, shuffle=True, batch_size=16)

# Define loss function (Contrastive Loss for similarity)
train_loss = losses.ContrastiveLoss(model=model)

# Fine-tune
model.fit(
    train_objectives=[(train_dataloader, train_loss)],
    epochs=3,
    warmup_steps=100,
    output_path=f'models/fine_tuned_{course_id}',
)

# Save model
model.save(f'models/fine_tuned_{course_id}')
```

### 4. Model Evaluation

Evaluate model performance using the benchmark suite.

```bash
# Run OCR benchmark
pytest tests/test_ocr_benchmark.py -v
```

#### Metrics
- **Accuracy**: Percentage of correct matches
- **Latency**: Average time per match (target: < 100ms)
- **Improvement**: Accuracy improvement over RapidFuzz baseline (target: > 10%)

#### Target Metrics
- Accuracy: > 85% on test set
- Latency: < 100ms per name (after model load)
- Improvement: > 10% over RapidFuzz alone

### 5. Model Deployment

#### Update Configuration

```yaml
# config/attendance.yaml
ml_matching:
  enabled: true
  model: models/fine_tuned_main  # Path to fine-tuned model
  cache_embeddings: true
  retrain_interval_days: 7
  threshold: 0.75
```

#### Warm Cache

```python
from attendance.ml_matcher import MLMatcher
from attendance.session_resolver import SessionResolver

# Initialize matcher
matcher = MLMatcher(model_name="models/fine_tuned_main")

# Warm cache with roster embeddings
resolver = SessionResolver(course_key="main")
roster = resolver.get_roster()
roster_names = [name for name, _ in roster]

# Pre-compute embeddings
for name in roster_names:
    matcher.get_embedding(name)
```

## Benchmarking

### Running Benchmarks

```bash
# Full benchmark suite
pytest tests/test_ocr_benchmark.py -v

# Individual tests
pytest tests/test_ocr_benchmark.py::TestOCRAccuracyBenchmark::test_rapidfuzz_baseline -v
pytest tests/test_ocr_benchmark.py::TestOCRAccuracyBenchmark::test_hybrid_matching_accuracy -v
pytest tests/test_ocr_benchmark.py::TestOCRAccuracyBenchmark::test_ocr_correction_effectiveness -v
pytest tests/test_ocr_benchmark.py::TestOCRAccuracyBenchmark::test_performance_comparison -v
```

### Benchmark Results Interpretation

#### RapidFuzz Baseline
- Shows baseline accuracy without ML
- Should be close to 100% for clear OCR
- Target: Maintain existing accuracy

#### Hybrid Matching
- Shows ML-only accuracy (without OCR corrections)
- Should be > 70% on test set
- Note: ML is meant to complement, not replace RapidFuzz

#### OCR Correction
- Shows effectiveness of learned corrections
- Should be 100% on test cases
- Real-world effectiveness depends on correction frequency

#### Performance Comparison
- Compares latency between RapidFuzz and ML
- ML is expected to be slower (~200ms vs ~0.2ms)
- Trade-off: Speed vs accuracy on edge cases

## Troubleshooting

### Model Loading Issues

**Problem**: Model fails to load or download
**Solution**:
- Check internet connection for first-time download
- Set `HF_TOKEN` environment variable for Hugging Face Hub
- Use local model path after initial download

### Low Accuracy

**Problem**: ML accuracy is below expected
**Solutions**:
- Check that training data quality is high (confirmations >= 2)
- Increase training epochs or use more training data
- Adjust similarity threshold (`threshold` parameter)
- Consider using a larger base model

### High Latency

**Problem**: ML matching is too slow
**Solutions**:
- Ensure `cache_embeddings=True` is set
- Warm cache with roster embeddings on startup
- Use a smaller model (MiniLM instead of MPNet)
- Batch multiple names in one request

### Overfitting

**Problem**: Model performs well on training data but poorly on new data
**Solutions**:
- Use more diverse training data
- Add negative examples (mismatched pairs)
- Reduce training epochs
- Use regularization (dropout, weight decay)

## Advanced Topics

### Cross-Validation

Use k-fold cross-validation to evaluate model generalization:

```python
from sklearn.model_selection import KFold

kf = KFold(n_splits=5)
for train_idx, val_idx in kf.split(train_examples):
    train_data = [train_examples[i] for i in train_idx]
    val_data = [train_examples[i] for i in val_idx]
    # Train and evaluate...
```

### Ensemble Methods

Combine multiple models for better accuracy:

```python
from sentence_transformers import SentenceTransformer

model1 = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
model2 = SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')

def ensemble_match(ocr_name, roster_names):
    emb1 = model1.encode(ocr_name)
    emb2 = model2.encode(ocr_name)
    # Average embeddings and compute similarity
    # ...
```

### A/B Testing

Compare fine-tuned model against baseline in production:

```python
# Use both models and log results
baseline_match = baseline_matcher.match(ocr_name, roster_names)
ml_match = ml_matcher.match(ocr_name, roster_names)

# Log both for comparison
log_result(ocr_name, baseline_match, ml_match, ground_truth)
```

## Maintenance

### Regular Retraining
- Schedule weekly retraining via `ml_matching.retrain_interval_days`
- Collect new aliases from review decisions
- Retrain and evaluate before deployment

### Model Versioning
- Save models with version numbers (e.g., `fine_tuned_main_v1`)
- Keep previous version for rollback
- Document training data and hyperparameters

### Monitoring
- Track match accuracy over time
- Monitor cache hit rates
- Alert on accuracy degradation

## References

- [Sentence-Transformers Documentation](https://www.sbert.net/)
- [Hugging Face Model Hub](https://huggingface.co/models)
- [Contrastive Loss Paper](https://arxiv.org/abs/1530.79041)
