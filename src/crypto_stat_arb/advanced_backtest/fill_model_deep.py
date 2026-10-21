"""Phase 3: deep survival model for fill probability estimation.

Architectural sketch
--------------------
A ConvTransformer discrete-time hazard model:

    Input  : feature sequence of length T (one tick per time step)
             features: [queue_ahead, depth_ratio, spread_pct, trade_sign,
                        short_vol, trade_count, regime_onehot(3)]  — dim 9
    Encoder: 1-D causal convolution stack (kernel 3, 4 layers, 64 channels)
             → captures local order-book dynamics
    Neck   : multi-head self-attention (4 heads, d_model=64)
             → long-range dependencies (queue draining, sweep patterns)
    Head   : per-horizon hazard rate sigmoid(W h_t + b)  → λ_t
    Output : S(h) = Π_{t=1}^{h} (1 - λ_t)  (discrete-time survival function)
             P(filled by h) = 1 - S(h)

Training
--------
Negative log-likelihood of a discrete-time hazard model with censoring:

    ℓ = Σ_i [ d_i · log λ_{T_i} + Σ_{t=1}^{T_i} log(1 - λ_t) ]

where d_i = 1 if order i was filled, T_i = observed horizon (fill or censor).

This module defines the interface and documents the architecture.
A full PyTorch implementation requires torch ≥ 2.0 and is not bundled here.
Wire it in by subclassing `DeepFillModelBase` and registering with
`DeepFillModelRegistry`.

Usage (once a concrete implementation exists)::

    model = DeepFillModelRegistry.get("conv_transformer_v1")
    model.load_weights("checkpoints/fill_model_v1.pt")
    prob = model.predict_fill_prob(features_sequence, horizon_s=10)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


# ── Feature spec ─────────────────────────────────────────────────────────────

FEATURE_DIM = 9
"""Dimensionality of the per-tick feature vector fed into the encoder.

    [0] queue_ahead (log1p-normalised)
    [1] depth_ratio  = local_depth / local_depth_baseline
    [2] spread_pct
    [3] trade_sign   (EMA of signed trade flow)
    [4] short_vol
    [5] trade_count  (recent)
    [6] regime_calm  (one-hot)
    [7] regime_elevated
    [8] regime_toxic
"""


# ── Base interface ────────────────────────────────────────────────────────────

class DeepFillModelBase(ABC):
    """Interface every Phase 3 fill model must implement."""

    @abstractmethod
    def load_weights(self, checkpoint_path: str) -> None:
        """Load pre-trained weights from a checkpoint file."""

    @abstractmethod
    def predict_fill_prob(
        self,
        features_sequence,   # array-like, shape (T, FEATURE_DIM)
        horizon_s: float,
    ) -> float:
        """Return P(filled within horizon_s seconds) given the feature history."""

    @abstractmethod
    def fit(
        self,
        records,             # list[FillLabelRecord] from fill_label.py
        epochs: int = 20,
        lr: float = 1e-3,
    ) -> dict[str, float]:
        """Train the model in-process.  Returns a dict of training metrics."""


# ── Registry ─────────────────────────────────────────────────────────────────

class DeepFillModelRegistry:
    """Maps short names to concrete DeepFillModelBase implementations."""

    _registry: dict[str, type[DeepFillModelBase]] = {}

    @classmethod
    def register(cls, name: str, model_cls: type[DeepFillModelBase]) -> None:
        cls._registry[name] = model_cls

    @classmethod
    def get(cls, name: str) -> DeepFillModelBase:
        if name not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none registered)"
            raise KeyError(
                f"No deep fill model named '{name}'. Available: {available}. "
                "Implement DeepFillModelBase and call "
                "DeepFillModelRegistry.register() to add one."
            )
        return cls._registry[name]()

    @classmethod
    def available(cls) -> list[str]:
        return sorted(cls._registry)


# ── Architecture config (for future ConvTransformer implementation) ───────────

@dataclass
class ConvTransformerConfig:
    """Hyperparameters for the reference ConvTransformer architecture.

    ConvTransformer layout
    ----------------------
    1. CausalConv1D stack: kernel=3, layers=4, channels=64, dilation doubles each layer
       → receptive field covers the most recent ~48 ticks
    2. LayerNorm + multi-head self-attention: n_heads=4, d_model=64
    3. Linear hazard head: sigmoid output per discrete time step
    4. Survival product: P(fill by h) = 1 - Π(1 - λ_t)

    Training details
    ----------------
    - Batch size: 512 (order sequences)
    - Sequence length T: 60 ticks (~1 min at 1s tick)
    - Optimizer: AdamW, lr=1e-3, weight decay=1e-4
    - LR schedule: cosine annealing, T_max = total_steps
    - Censoring: handled via the discrete-time log-likelihood (see module docstring)
    - Label smoothing: ε=0.05 on the hazard targets
    """
    d_model: int = 64
    n_conv_layers: int = 4
    conv_kernel: int = 3
    n_heads: int = 4
    sequence_len: int = 60
    dropout: float = 0.1
    label_smoothing: float = 0.05
