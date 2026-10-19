"""Bin packing solver. Edit me.

pack(items, capacity) gets item sizes and returns a list of bins, each a list of item
indices, with every index used exactly once and no bin over capacity.
"""


def pack(items, capacity):
    # next fit: open a new bin whenever the current one can't take the item
    bins, load = [[]], 0
    for i, size in enumerate(items):
        if load + size > capacity:
            bins.append([])
            load = 0
        bins[-1].append(i)
        load += size
    return bins
