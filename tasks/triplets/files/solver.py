"""Bin packing solver. Edit me.

pack(items, capacity) gets item sizes and returns a list of bins, each a list of item
indices, with every index used exactly once and no bin over capacity.
"""


def pack(items, capacity):
    # first fit: put each item into the first bin with room
    bins, loads = [], []
    for i, size in enumerate(items):
        for k, load in enumerate(loads):
            if load + size <= capacity:
                bins[k].append(i)
                loads[k] += size
                break
        else:
            bins.append([i])
            loads.append(size)
    return bins
