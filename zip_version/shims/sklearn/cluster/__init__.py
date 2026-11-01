"""sklearn.cluster 兼容层:只提供 BabelDOC 用到的 DBSCAN。

纯 numpy 实现,支持 euclidean/manhattan 度量。BabelDOC 的两处调用都是
min_samples=1(每个点都是核心点,结果=曼哈顿距离 ≤ eps 的连通分量,按首成员
顺序编号),与 sklearn 的标签语义逐值一致(对拍见
zip_version/tests/test_shim_parity.py);min_samples>1 的经典 DBSCAN 语义
(噪声=-1、边界点归第一个到达它的簇)也已实现并纳入对拍。
"""

from __future__ import annotations

import numpy as np

__all__ = ["DBSCAN"]


class DBSCAN:
    def __init__(
        self,
        eps=0.5,
        *,
        min_samples=5,
        metric="euclidean",
        metric_params=None,
        algorithm="auto",
        leaf_size=30,
        p=None,
        n_jobs=None,
    ):
        if metric_params:
            raise NotImplementedError("sklearn shim: 不支持 metric_params")
        metric = {"cityblock": "manhattan", "l1": "manhattan"}.get(metric, metric)
        if metric not in ("euclidean", "manhattan"):
            raise NotImplementedError(f"sklearn shim: 不支持 metric={metric!r}")
        if p is not None and p != 2:
            raise NotImplementedError("sklearn shim: 仅支持 p=2(euclidean)")
        self.eps = float(eps)
        self.min_samples = int(min_samples)
        self.metric = metric
        # algorithm/leaf_size/n_jobs 只是实现细节,接受并忽略
        self.algorithm = algorithm
        self.leaf_size = leaf_size
        self.n_jobs = n_jobs

    def fit(self, X, y=None, sample_weight=None):
        if sample_weight is not None:
            raise NotImplementedError("sklearn shim: 不支持 sample_weight")
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        n = X.shape[0]
        if n == 0:
            self.labels_ = np.empty(0, dtype=np.int64)
            self.core_sample_indices_ = np.empty(0, dtype=np.int64)
            self.components_ = np.empty_like(X)
            return self

        if self.metric == "manhattan":
            dist = np.abs(X[:, None, :] - X[None, :, :]).sum(axis=2)
        else:
            dist = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(axis=2))
        neighbors = dist <= self.eps  # 含自身(距离 0),与 sklearn 半径语义一致
        core = neighbors.sum(axis=1) >= self.min_samples

        labels = np.full(n, -1, dtype=np.int64)
        visited = np.zeros(n, dtype=bool)
        cluster = 0
        for i in range(n):
            if visited[i] or not core[i]:
                visited[i] = True
                continue
            labels[i] = cluster
            visited[i] = True
            queue = [i]
            while queue:
                j = queue.pop()
                for k in np.flatnonzero(neighbors[j]):
                    if not visited[k]:
                        visited[k] = True
                        if core[k]:
                            queue.append(int(k))
                    if labels[k] == -1:
                        labels[k] = cluster
            cluster += 1

        self.labels_ = labels
        self.core_sample_indices_ = np.flatnonzero(core).astype(np.int64)
        self.components_ = X[self.core_sample_indices_]
        return self

    def fit_predict(self, X, y=None, sample_weight=None):
        self.fit(X, y=y, sample_weight=sample_weight)
        return self.labels_
