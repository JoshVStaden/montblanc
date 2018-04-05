import collections
from functools import partial
import itertools
import os
import sys

import boltons.cacheutils
import cppimport
import dask
import dask.array as da
from dask.array.core import getter
import numpy as np
import six
try:
    import cytoolz as toolz
except ImportError:
    import toolz
import xarray as xr
from xarray_ms import xds_from_ms, xds_from_table

import montblanc
from montblanc.src_types import source_types

dsmod = cppimport.imp('montblanc.ext.dataset_mod')

def _create_if_not_present(ds, attr, default_fn):
    """
    Retrieves `attr` from `ds` if present, otherwise
    creates it with `default_fn`
    """
    try:
        data = getattr(ds, attr)
    except AttributeError:
        # Create the attribute with default_fn and assign to the dataset
        schema = ds.attrs['schema'][attr]
        data = default_fn(ds, schema)
        ds.assign(**{attr: xr.DataArray(data, dims=schema["dims"])})

        return data.compute()

    return data.values

def default_base_ant_pairs(antenna, auto_correlations=False):
    """ Compute base antenna pairs """
    k = 0 if auto_correlations == True else 1
    return np.triu_indices(antenna, k)

def default_antenna1(ds, schema):
    """ Default antenna 1 """
    ap = default_base_ant_pairs(ds.dims['antenna'],
                                ds.attrs['auto_correlations'])
    return da.tile(ap[0], ds.dims['utime']).rechunk(schema['chunks'])

def default_antenna2(ds, schema):
    """ Default antenna 2 """
    ap = default_base_ant_pairs(ds.dims['antenna'],
                                ds.attrs['auto_correlations'])
    return da.tile(ap[1], ds.dims['utime']).rechunk(schema['chunks'])

def default_time_unique(ds, schema):
    """ Default unique time """
    return da.linspace(4.865965e+09, 4.865985e+09,
                        schema["shape"][0],
                        chunks=schema["chunks"][0])

def default_time_offset(ds, schema):
    """ Default time offset """
    vrow, utime = (ds.dims[k] for k in ('vrow', 'utime'))

    bl = vrow // utime
    assert utime*bl == vrow
    return da.arange(utime,chunks=schema["chunks"])*bl

def default_time_vrow_chunks(ds, schema):
    """ Default visibility row chunks for each timestep """
    vrow, utime = (ds.dims[k] for k in ('vrow', 'utime'))

    bl = vrow // utime
    assert utime*bl == vrow
    return da.full(schema["shape"], bl, chunks=schema["chunks"])

def default_time_arow_chunks(ds, schema):
    """ Default antenna row chunks for each timestep """

    antenna1 = _create_if_not_present(ds, 'antenna1', default_antenna1)
    antenna2 = _create_if_not_present(ds, 'antenna2', default_antenna2)
    time_vrow_chunks = _create_if_not_present(ds, 'time_vrow_chunks',
                                                default_time_vrow_chunks)

    start = 0
    time_arow_chunks = []

    for chunk in time_vrow_chunks:
        end = start + chunk
        a1 = antenna1[start:end]
        a2 = antenna2[start:end]
        time_arow_chunks.append(len(np.unique(np.append(a1,a2))))

        start = end

    time_arow_chunks = np.asarray(time_arow_chunks, dtype=np.int32)
    return da.from_array(time_arow_chunks, chunks=schema["chunks"])

def default_time(ds, schema):
    """ Default time """

    time_unique = _create_if_not_present(ds, "time_unique",
                                        default_time_unique)
    time_vrow_chunks = _create_if_not_present(ds, "time_vrow_chunks",
                                        default_time_vrow_chunks)

    # Must agree
    if not len(time_vrow_chunks) == len(time_unique):
        raise ValueError("Number of time chunks '%d' "
                        "and unique timestamps '%d' "
                        "do not agree" % (len(time_vrow_chunks), len(time_unique)))

    return da.concatenate([da.full(tc, ut, dtype=schema['dtype'], chunks=tc) for ut, tc
                        in zip(time_unique, time_vrow_chunks)]).rechunk(schema['chunks'])

def default_time_index(ds, schema):
    # Try get time_vrow_chunks off the dataset first
    # otherwise generate from scratch

    time_vrow_chunks = _create_if_not_present(ds, "time_vrow_chunks",
                                        default_time_vrow_chunks)

    time_index_chunks = []
    start = 0

    for i, c in enumerate(time_vrow_chunks):
        time_index_chunks.append(da.full(c, i, dtype=schema['dtype'], chunks=c))
        start += c

    return da.concatenate(time_index_chunks).rechunk(schema['chunks'])

def default_frequency(ds, schema):
    return da.linspace(8.56e9, 2*8.56e9, schema["shape"][0],
                                    chunks=schema["chunks"][0])

def is_power_of_2(n):
    return n != 0 and ((n & (n-1)) == 0)

def identity_on_dim(ds, schema, dim):
    """ Return identity matrix on specified dimension """
    rshape = schema["shape"]
    shape = schema["dims"]

    dim_idx = shape.index(dim)
    dim_size = rshape[dim_idx]

    # Require a power of 2
    if not is_power_of_2(dim_size):
        raise ValueError("Dimension '%s' of size '%d' must be a power of 2 "
                        "for broadcasting the identity" % (dim, dim_size))

    # Create index to introduce new dimensions for broadcasting
    it = six.moves.range(len(shape))
    idx = tuple(slice(None) if i == dim_idx else None for i in it)

    # Broadcast identity matrix and rechunk
    identity = [1] if dim_size == 1 else [1] + [0]*(dim_size-2) + [1]
    identity = np.array(identity, dtype=schema["dtype"])[idx]
    return da.broadcast_to(identity, rshape).rechunk(schema["chunks"])

def one_jansky_stokes(ds, schema, dim):
    """ Return one jansky stokes on the specified dimension """
    dims = schema["dims"]
    shape = schema["shape"]

    dim_idx = dims.index(dim)
    dim_size = shape[dim_idx]

    repeat = dim_size-1
    repeat = 0 if repeat < 0 else repeat

    stokes = np.array([1] + [0]*repeat, dtype=schema["dtype"])
    return da.broadcast_to(stokes, shape).rechunk(schema["chunks"])

def default_gaussian(ds, schema):
    gauss_shape = np.array([[0],[0],[1]], dtype=schema["dtype"])
    return da.broadcast_to(gauss_shape, schema["shape"]).rechunk(schema["chunks"])

default_sersic = default_gaussian

def internal_schema():
    return {
        "__point_keys" : {
            "dims": (None,),
            "dtype": np.int64,
        },
        "__gaussian_keys" : {
            "dims": (None,),
            "dtype": np.int64,
        },
        "__sersic_keys" : {
            "dims": (None,),
            "dtype": np.int64,
        },
    }

def source_schema():
    return {
        "point_lm": {
            "dims": ("point", "(l,m)"),
            "dtype": np.float64,
        },
        "point_ref_freq": {
            "dims" : ("point",),
            "dtype": np.float64,
        },
        "point_alpha": {
            "dims": ("point", "utime"),
            "dtype": np.float64,
        },
        "point_stokes": {
            "dims": ("point", "utime", "(I,Q,U,V)"),
            "dtype": np.float64,
            "default": partial(one_jansky_stokes, dim="(I,Q,U,V)"),
        },

        "gaussian_lm": {
            "dims": ("gaussian", "(l,m)"),
            "dtype": np.float64,
        },
        "gaussian_ref_freq": {
            "dims": ("gaussian",),
            "dtype": np.float64,
        },
        "gaussian_alpha": {
            "dims": ("gaussian", "utime"),
            "dtype": np.float64,
        },
        "gaussian_stokes": {
            "dims": ("gaussian", "utime", "(I,Q,U,V)"),
            "dtype": np.float64,
            "default": partial(one_jansky_stokes, dim="(I,Q,U,V)"),
        },
        "gaussian_shape_params": {
            "dims": ("(lproj,mproj,theta)", "gaussian"),
            "dtype": np.float64,
            "default": default_gaussian,
        },

        "sersic_lm": {
            "dims": ("sersic", "(l,m)"),
            "dtype": np.float64,
        },
        "sersic_alpha": {
            "dims": ("sersic", "utime"),
            "dtype": np.float64,
        },
        "sersic_stokes": {
            "dims": ("sersic", "utime", "(I,Q,U,V)"),
            "dtype": np.float64,
            "default": partial(one_jansky_stokes, dim="(I,Q,U,V)"),
        },
        "sersic_ref_freq": {
            "dims": ("sersic",),
            "dtype": np.float64,
        },
        "sersic_shape_params": {
            "dims": ("(s1,s2,theta)", "sersic"),
            "dtype": np.float64,
            "default": default_sersic,
        },

    }

def default_schema():
    return {
        "time" : {
            "dims": ("vrow",),
            "dtype": np.float64,
            "default": default_time,
        },

        "time_index": {
            "dims": ("vrow",),
            "dtype": np.int32,
            "default": default_time_index,
        },

        "time_unique": {
            "dims": ("utime",),
            "dtype": np.float64,
            "default": default_time_unique,
        },

        "time_arow_chunks" : {
            "dims": ("utime",),
            "dtype": np.int32,
            "default": default_time_arow_chunks,
        },

        "time_vrow_chunks" : {
            "dims": ("utime",),
            "dtype": np.int32,
            "default": default_time_vrow_chunks,
        },

        "base_vis": {
            "dims": ("vrow", "chan", "corr"),
            "dtype": np.complex128,
        },

        "data": {
            "dims": ("vrow", "chan", "corr"),
            "dtype": np.complex128,
        },

        "antenna_uvw": {
            "dims": ("arow", "(u,v,w)"),
            "dtype": np.float64,
        },

        "antenna1" : {
            "dims": ("vrow",),
            "dtype": np.int32,
            "default": default_antenna1,
        },

        "antenna2" : {
            "dims": ("vrow",),
            "dtype": np.int32,
            "default": default_antenna2,
        },

        "flag": {
            "dims": ("vrow", "chan", "corr"),
            "dtype": np.uint8,
            "default": lambda ds, as_: da.zeros(shape=as_["shape"],
                                                dtype=as_["dtype"],
                                                chunks=as_["chunks"])
        },

        "weight": {
            "dims": ("vrow", "chan", "corr"),
            "dtype": np.float64,
            "default": lambda ds, as_: da.ones(shape=as_["shape"],
                                                dtype=as_["dtype"],
                                                chunks=as_["chunks"])
        },

        "frequency": {
            "dims": ("chan",),
            "dtype": np.float64,
            "default": default_frequency,
        },

        "parallactic_angles": {
            "dims": ("arow",),
            "dtype": np.float64,
        },

        "antenna_position": {
            "dims": ("antenna", "(x,y,z)"),
            "dtype": np.float64,
        },

        "direction_independent_effects": {
            "dims": ("arow", "chan", "corr"),
            "dtype": np.complex128,
            "default": partial(identity_on_dim, dim="corr")
        },

        # E beam cube
        "ebeam": {
            "dims": ("beam_lw", "beam_mh", "beam_nud", "corr"),
            "dtype": np.complex128,
            "default": partial(identity_on_dim, dim="corr")
        },

        "pointing_errors": {
            "dims": ("arow", "chan", "(l,m)"),
            "dtype": np.float64,
        },

        "antenna_scaling": {
            "dims": ("antenna", "chan", "(l,m)"),
            "dtype": np.float64,
            "default": lambda ds, as_: da.ones(shape=as_["shape"],
                                                dtype=as_["dtype"],
                                                chunks=as_["chunks"])
        },

        "beam_extents": {
            "dims": ("(ll,lm,lf,ul,um,uf)",),
            "dtype": np.float64,
            "default": lambda ds, as_: da.from_array(
                np.array([0,0,0,1,1,1], dtype=as_["dtype"]),
                chunks=as_["chunks"])
        },

        "beam_freq_map": {
            "dims": ("beam_nud",),
            "dtype": np.float64,
            "default": default_frequency,
        },
    }

def input_schema():
    """ Montblanc input schemas """
    return toolz.merge(default_schema(), source_schema())

def scratch_schema():
    """ Intermediate outputs produced by tensorflow operators """
    return {
        # TODO(sjperkins) "point" dimension used to specify number of
        # sources in general, so meaning applies to "gaussians" and
        # "sersics" too. This will be confusing at some point and
        # "should be changed".
        "bsqrt": {
            "dims": ("point", "utime", "chan", "corr"),
            "dtype": np.complex128,
        },

        "complex_phase": {
            "dims": ("point", "arow", "chan"),
            "dtype": np.complex128,
        },

        "ejones": {
            "dims": ("point", "arow", "chan", "corr"),
            "dtype": np.complex128,
        },

        "antenna_jones": {
            "dims": ("point", "arow", "chan", "corr"),
            "dtype": np.complex128,
        },

        "sgn_brightness": {
            "dims": ("point", "utime"),
            "dtype": np.int8,
        },

        "source_shape": {
            "dims": ("point", "vrow", "chan"),
            "dtype": np.float64,
        },

        "chi_sqrd_terms": {
            "dims": ("vrow", "chan"),
            "dtype": np.float64,
        }
    }

def output_schema():
    """ Montblanc output schemas """
    return {
        "model_vis": {
            "dims": ('vrow', 'chan', 'corr'),
            "dtype": np.complex128,
        },
        "chi_squared": {
            "dims": (),
            "dtype": np.float64,
        },
    }

def default_dim_sizes(dims=None):
    """ Returns a dictionary of default dimension sizes """

    ds = {
        '(I,Q,U,V)': 4,
        '(x,y,z)': 3,
        '(u,v,w)': 3,
        'utime': 100,
        'chan': 64,
        'corr': 4,
        'pol': 4,
        'antenna': 7,
        'spw': 1,
    }

    # Derive vrow from baselines and unique times
    nbl = ds['antenna']*(ds['antenna']-1)//2
    ds.update({'arow': ds['utime']*ds['antenna'] })
    ds.update({'vrow': ds['utime']*nbl })

    # Source dimensions
    ds.update({
        'point': 1,
        'gaussian': 0,
        'sersic': 0,
        '(l,m)': 2,
        '(lproj,mproj,theta)': 3,
        '(s1,s2,theta)': 3,
    })

    # Beam dimensions
    ds.update({
        'beam_lw': 10,
        'beam_mh': 10,
        'beam_nud': 10,
        '(ll,lm,lf,ul,um,uf)': 6,
    })

    if dims is not None:
        ds.update(dims)

    return ds

def default_dataset(xds=None, dims=None):
    """
    Creates a default montblanc :class:`xarray.Dataset`.
    If `xds` is supplied, missing arrays will be filled in
    with default values.

    Parameters
    ----------
    xds (optional) : :class:`xarray.Dataset`
    dims (optional) : dict
        Dictionary of dimensions

    Returns
    -------
    :class:`xarray.Dataset`
    """

    dims = default_dim_sizes(dims)

    in_schema = toolz.merge(default_schema(), source_schema())

    if xds is None:
        # Create coordinates for each dimension
        coords = { k: np.arange(dims[k]) for k in dims.keys() }
        # Create a dummy arrays for arow and vrow dimensions
        # Needed for most default methods
        arrays = { "__dummy_vrow__" : xr.DataArray(da.ones(shape=dims["vrow"],
                                                        chunks=10000,
                                                        dtype=np.float64),
                                                dims=["vrow"]) }
        arrays = { "__dummy_arow__" : xr.DataArray(da.ones(shape=dims["arow"],
                                                        chunks=10000,
                                                        dtype=np.float64),
                                                dims=["arow"]) }


        xds = xr.Dataset(arrays, coords=coords)
    else:
        # Create coordinates for default dimensions
        # not present on the dataset
        coords = { k: np.arange(dims[k]) for k in dims.keys()
                                        if k not in xds.dims }

        # Update dimension dictionary with dataset dimensions
        dims.update(xds.dims)

        # Assign coordinates
        xds.assign_coords(**coords)

    default_attrs = { 'schema': in_schema,
                       'auto_correlations': False }

    default_attrs.update(xds.attrs)
    xds.attrs.update(default_attrs)

    arrays = xds.data_vars.keys()
    missing_arrays = set(in_schema).difference(arrays)

    chunks = xds.chunks

    # Create reified shape and chunks on missing array schemas
    for n in missing_arrays:
        schema = in_schema[n]
        sshape = schema["dims"]
        schema["shape"] = rshape = tuple(dims.get(d, d) for d in sshape)
        schema["chunks"] = tuple(chunks.get(d, r) for d, r in zip(sshape, rshape))

    def _default_zeros(ds, schema):
        """ Return a dask array of zeroes """
        return da.zeros(shape=schema["shape"],
                       chunks=schema["chunks"],
                        dtype=schema["dtype"])

    new_arrays = {}

    for n in missing_arrays:
        # While creating missing arrays, other missing arrays
        # may be created
        if n in xds:
            continue

        schema = in_schema[n]
        default = schema.get('default', _default_zeros)
        new_arrays[n] = xr.DataArray(default(xds, schema), dims=schema["dims"])

    xds = xds.assign(**new_arrays)

    # Drop dummy arrays if present
    drops = [a for a in ("__dummy_vrow__", "__dummy_arow__") if a in xds]
    xds = xds.drop(drops)

    return xds

def create_antenna_uvw(xds):
    """
    Adds `antenna_uvw` coordinates to the given :class:`xarray.Dataset`.

    Parameters
    ----------
    xds : :class:`xarray.Dataset`
        base Dataset.

    Notes
    -----
    This methods **depends** on the `vrow` and `utime` chunking in `xds`
    being correct. Put as simply as possible, the consecutive unique
    timestamps referenced by chunks in the `utime` dimension must be
    associated with consecutive chunks in the `vrow` dimension.

    Returns
    -------
    :class:`xarray.Dataset`
        `xds` with `antenna_uvw` assigned.
    """
    from functools import partial

    def _chunk_iter(chunks):
        """ Return dimension slices given a list of chunks """
        start = 0
        for size in chunks:
            end = start + size
            yield slice(start, end)
            start = end

    chunks = xds.chunks
    utime_groups = chunks['utime']
    vrow_groups = chunks['vrow']
    time_vrow_chunks = xds.time_vrow_chunks

    token = dask.base.tokenize(xds.uvw, xds.antenna1, xds.antenna2,
                            xds.time_vrow_chunks, vrow_groups, utime_groups)
    name = "-".join(("create_antenna_uvw", token))
    p_ant_uvw = partial(dsmod.antenna_uvw, nr_of_antenna=xds.dims["antenna"])

    it = itertools.izip(_chunk_iter(vrow_groups), _chunk_iter(utime_groups))
    dsk = {}

    # Create the dask graph
    for i, (rs, uts) in enumerate(it):
        dsk[(name, i, 0, 0)] = (p_ant_uvw,
                                (getter, xds.uvw, rs),
                                # TODO(sjperkins). This corrects conjugation
                                # output visibilities. Fix antenna_uvw to
                                # take antenna1 + antenna2
                                (getter, xds.antenna2, rs),
                                (getter, xds.antenna1, rs),
                                (getter, xds.time_vrow_chunks, uts))

        # Sanity check
        if not np.sum(time_vrow_chunks[uts]) == rs.stop - rs.start:
            sum_chunks = np.sum(time_vrow_chunks[uts])
            raise ValueError("Sum of time_vrow_chunks[%d:%d] '%d' "
                            "does not match the number of vrows '%d' "
                            "in the vrow[%d:%d]" %
                                (uts.start, uts.stop, sum_chunks,
                                rs.stop-rs.start,
                                rs.start, rs.stop))

    # Chunks for 'utime', 'antenna' and 'uvw' dimensions
    chunks = (tuple(utime_groups),
                (xds.dims["antenna"],),
                (xds.dims["(u,v,w)"],))

    # Create dask array and assign it to the dataset
    dask_array = da.Array(dsk, name, chunks, xds.uvw.dtype)
    dims = ("utime", "antenna", "(u,v,w)")
    return xds.assign(antenna_uvw=xr.DataArray(dask_array, dims=dims))

def dataset_from_ms(ms):
    """
    Creates an xarray dataset from the given Measurement Set

    Returns
    -------
    `xarray.Dataset`
        Dataset with MS columns as arrays
    """

    renames = { 'rows': 'vrow',
                'chans': 'chan',
                'pols': 'pol',
                'corrs': 'corr',
                'time_chunks' : 'time_vrow_chunks'}

    xds = xds_from_ms(ms).rename(renames)
    xads = xds_from_table("::".join((ms, "ANTENNA")), table_schema="ANTENNA")
    xspwds = xds_from_table("::".join((ms, "SPECTRAL_WINDOW")), table_schema="SPECTRAL_WINDOW")
    xds = xds.assign(antenna_position=xads.rename({"rows" : "antenna"}).drop('msrows').position,
                    frequency=xspwds.rename({"rows":"spw", "chans" : "chan"}).drop('msrows').chan_freq[0])
    return xds

def merge_dataset(iterable):
    """
    Merge datasets. Dataset dimensions and coordinates must match.
    Later datasets have precedence.

    Parameters
    ----------
    iterable : :class:`xarray.Dataset` or iterable of :class:`xarray.Dataset`
        Datasets to merge

    Returns
    -------
    :class:`xarray.Dataset`
        Merged dataset

    """
    if not isinstance(iterable, collections.Sequence):
        iterable = [iterable]

    # Construct lists of sizes and coordinates for each dimension
    dims = collections.defaultdict(list)
    coords = collections.defaultdict(list)

    for i, ds in enumerate(iterable):
        for dim, size in ds.dims.iteritems():
            # Record dataset index
            dims[dim].append(DimensionInfo(i, size))

        for dim, coord in ds.coords.iteritems():
            coords[dim].append(DimensionInfo(i, coord.values))

    # Sanity check dimension matches on all datasets
    for name, dim_sizes in dims.iteritems():
        if not all(dim_sizes[0].info == ds.info for ds in dim_sizes[1:]):
            msg_str = ','.join(['(dataset=%d,%s=%d)' % (ds.index, name, ds.info)
                                                            for ds in dim_sizes])

            raise ValueError("Conflicting dataset dimension sizes for "
                            "dimension '{n}'. '{ds}'".format(n=name, ds=msg_str))

    # Sanity check dimension coordinates matches on all datasets
    for name, coord in coords.iteritems():
        compare = [(coord[0].info == co.info).all() for co in coord]
        if not all(compare):
            msg_str = ','.join(["(dataset %d '%s' coords match 0: %s)" % (co.index, name, c)
                                            for co, c in zip(dim_sizes, compare)])

            raise ValueError("Conflicting dataset coordinates for "
                            "dimension '{n}'. {m}".format(n=name, m=msg_str))

    # Create dict of data variables for merged datsets
    # Last dataset has precedence
    data_vars = { k: v for ds in iterable
                    for k, v in ds.data_vars.items() }

    # Merge attributes
    attrs = toolz.merge(ds.attrs for ds in iterable)

    return xr.Dataset(data_vars, attrs=attrs)


def group_vrow_chunks(xds, max_arow=1000, max_vrow=100000):
    """
    Return a dictionary of unique time, vrow and arow groups.
    Groups are formed by accumulating chunks in the
    `time_vrow_chunks` array attached to `xds` until
    either `max_arow` or `max_vrow` is reached.

    Parameters
    ----------
    xds : :class:`xarray.Dataset`
        Dataset with `time_vrow_chunks` member
    max_arow (optional) : integer
        Maximum antenna row group size
    max_vrow (optional) : integer
        Maximum visibility row group size

    Returns
    -------
    dict
        { 'utime': (time_group_1, ..., time_group_n),
          'arow': (arow_group_1, ..., arow_group_n),
          'vrow': (vrow_group_1, ..., vrow_group_n) }
    """
    vrow_groups = [0]
    utime_groups = [0]
    arow_groups = [0]
    arows = 0
    vrows = 0
    utimes = 0

    vrow_chunks = xds.time_vrow_chunks.values
    arow_chunks = xds.time_arow_chunks.values

    for arow_chunk, vrow_chunk in zip(arow_chunks, vrow_chunks):
        next_vrow = vrows + vrow_chunk
        next_arow = arows + arow_chunk

        if next_vrow > max_vrow:
            arow_groups.append(arows)
            vrow_groups.append(vrows)
            utime_groups.append(utimes)

            arows = arow_chunk
            vrows = vrow_chunk
            utimes = 1
        else:
            arows = next_arow
            vrows = next_vrow
            utimes += 1

    if vrows > 0:
        vrow_groups.append(vrows)
        utime_groups.append(utimes)
        arow_groups.append(arows)

    return {
        'utime': tuple(utime_groups[1:]),
        'vrow': tuple(vrow_groups[1:]),
        'arow': tuple(arow_groups[1:])
    }

def montblanc_dataset(xds=None):
    """
    Massages an :class:`xarray.Dataset` produced by `xarray-ms` into
    a dataset expected by montblanc.

    Returns
    -------
    `xarray.Dataset`
    """
    if xds is None:
        return default_dataset()

    schema = input_schema()
    required_arrays = set(schema.keys())

    # Assign weight_spectrum to weight, if available
    if "weight_spectrum" in xds:
        mds = xds.assign(weight=xds.weight_spectrum)
    # Otherwise broadcast weight up to weight spectrum dimensionality
    elif "weight" in xds:
        dims = xds.dims
        chunks = xds.chunks
        weight_dims = schema['weight']['dims']
        shape = tuple(dims[d] for d in weight_dims)
        chunks = tuple(chunks[d] for d in weight_dims)
        weight = da.broadcast_to(xds.weight.data[:,None,:], shape).rechunk(chunks)
        mds = xds.assign(weight=xr.DataArray(weight, dims=weight_dims))

    _create_if_not_present(mds, "time_arow_chunks", default_time_arow_chunks)

    # Fill in any default arrays
    mds = default_dataset(mds)

    # At this point, our vrow chunking strategy is whatever
    # came out of the original dataset. This will certainly
    # cause breakages in create_antenna_uvw
    # because vrows need to be grouped together
    # per-unique timestep. Perform this chunking operation now.
    max_vrow = max(mds.chunks['vrow'])
    chunks = group_vrow_chunks(mds, max_vrow=max_vrow)
    mds = mds.chunk(chunks)

    # Derive antenna UVW coordinates.
    # This depends on above chunking strategy
    mds = create_antenna_uvw(mds)

    return mds
    # Drop any superfluous arrays and return
    return mds.drop(set(mds.data_vars.keys()).difference(required_arrays))

def budget(schemas, dims, mem_budget, reduce_fn):
    """
    Reduce dimension values in `dims` according to
    strategy specified in generator `reduce_fn`
    until arrays in `schemas` fit within specified `mem_budget`.

    Parameters
    ----------
    schemas : dict or sequence of dict
        Dictionary of array schemas, of the form
        :code:`{name : {"dtype": dtype, "dims": (d1,d2,...,dn)}}`
    dims : dict
        Dimension size mapping, of the form
        :code:`{"d1": i, "d2": j, ..., "dn": k}
    mem_budget : int
        Number of bytes defining the memory budget
    reduce_fn : callable
        Generator yielding a lists of dimension reduction tuples.
        For example:

        .. code-block:: python

            def red_gen():
                yield [('utime', 100), ('vrow', 10000)]
                yield [('utime', 50), ('vrow', 1000)]
                yield [('utime', 20), ('vrow', 100)]

    Returns
    -------
    dict
        A :code:`{dim: size}` mapping of
        dimension reductions that fit the
        schema within the memory budget.
    """

    # Promote to list
    if not isinstance(schemas, (tuple, list)):
        schemas = [schemas]

    array_details = {n: (a['dims'], np.dtype(a['dtype']))
                                for schema in schemas
                                for n, a in schema.items() }

    applied_reductions = {}

    def get_bytes(dims, arrays):
        """ Get number of bytes in the dataset """
        return sum(np.product(tuple(dims[d] for d in a[0]))*a[1].itemsize
                                                for a in arrays.values())

    bytes_required = get_bytes(dims, array_details)

    for reduction in reduce_fn():
        if bytes_required > mem_budget:
            for dim, size in reduction:
                dims[dim] = size
                applied_reductions[dim] = size

            bytes_required = get_bytes(dims, array_details)
        else:
            break

    return applied_reductions

def rechunk_to_budget(mds, mem_budget, reduce_fn=None):
    """
    Rechunk `mds` dataset so that the memory required to
    solve a tile of the RIME fits within `mem_budget`.

    This function calls :func:`budget` internally.

    Note that this tile might be substantially larger than
    the same tile on the dataset as it incorporates temporary
    output arrays.

    A custom `reduce_fn` function can be supplied.

    Parameters
    ----------
    mds : :class:`xarray.Dataset`
        Dataset to rechunk
    mem_budget : integer
        Memory budget in bytes required to **solve
        the RIME**.
    reduce_fn (optional) : callable
        A reduction function, as documented in :func:`budget`

    Returns
    -------
    :class:`xarray.Dataset`
        A Dataset chunked so that a dataset tile
        required to solve the RIME fits within specified
        memory_budget `mem_budget`.

    """
    if reduce_fn is None:
        reduce_fn = _reduction

    dims = mds.dims

    ar = budget([input_schema(), scratch_schema(), output_schema()],
                dict(dims), mem_budget, partial(reduce_fn, mds))

    max_vrows = ar.get('vrow', max(mds.antenna1.data.chunks[0]))
    grc = group_vrow_chunks(mds, max_vrows)

    for k, v in ar.items():
        print k, v, dims[k]
        print da.core.normalize_chunks(v, (dims[k],))[0]

    ar = { k: da.core.normalize_chunks(v, (dims[k],))[0]
                                for k, v in ar.items() }
    ar.update(grc)
    return mds.chunk(ar)

def _uniq_log2_range(start, size, div):
    """
    Produce unique integers in the start, start+size range
    with a log2 distribution
    """
    start = np.log2(start)
    size = np.log2(size)
    int_values = np.int32(np.logspace(start, size, div, base=2)[:-1])

    return np.flipud(np.unique(int_values))

def _reduction(xds):
    """ Default reduction """
    dims = xds.dims

    st = source_types()
    sources = max(dims[s] for s in st)

    # Try reducing to 50 sources first (of each type)
    if sources > 50:
        yield [(s, 50) for s in st]

    # Then reduce by unique times 'utime'.
    # This implicitly reduce the number of
    # visibility 'vrows' and antenna 'arows'
    # associated with each 'utime' data point.
    utimes = _uniq_log2_range(1, dims['utime'], 50)

    for utime in utimes:
        vrows = xds.time_vrow_chunks[:utime].values.sum()
        arows = xds.time_arow_chunks[:utime].values.sum()
        yield [('utime', utime), ('vrow', vrows), ('arow', arows)]

if __name__ == "__main__":
    from pprint import pprint
    xds = montblanc_dataset()
    print xds

    xds = dataset_from_ms("~/data/D147-LO-NOIFS-NOPOL-4M5S.MS")
    mds = montblanc_dataset(xds)

    # Test antenna_uvw are properly computed. Do not delete!
    print mds.antenna_uvw.compute()

    mds = rechunk_to_budget(mds, 2*1024*1024*1024, _reduction)

