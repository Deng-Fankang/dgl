"""Module for SPMV rules."""
from __future__ import absolute_import

from ..base import DGLError
from .. import backend as F
from .. import utils

from . import ir
from .ir import var as var

def analyze_v2v_spmv(graph, mfunc, rfunc):
    """Analyze if SPMV from node space to node space can be applied.

    Parameters
    ----------
    graph: DGLGraph
        DGLGraph to use
    mfunc : list of dgl.function.BuiltinFunction
        The message function list.
    rfunc : list of dgl.function.BuiltinFunction
        The reduce function list.

    Returns
    -------
    spmv_pairs : list of pair of builtin functions
        The pair of spvm applicable message/reduce functions.
    mfunc_left: list
        A list of message functions that can't use v2v spmv. In other
        words, these message functions need to be materialized
    rfunc_left: list
        A list of reduce functions that can't use v2v spmv
    """
    spmv_pairs = []
    mfunc_left = []
    rfunc_left = []

    fld2mfunc = {fn.out_field: fn for fn in mfunc}
    touched_mfld = set()

    for rfn in rfunc:
        mfld = rfn.msg_field
        if mfld not in fld2mfunc:
            raise DGLError('Reduce function requires message field "%s",'
                           ' but no message function generates it.' % mfld)
        mfn = fld2mfunc[mfld]
        # TODO(minjie): should pre-compile a look up table
        if mfn.is_spmv_supported(graph) and rfn.is_spmv_supported():
            spmv_pairs.append((mfn, rfn))
        else:
            if mfld not in touched_mfld:
                touched_mfld.add(mfld)
                mfunc_left.append(mfn)
            rfunc_left.append(rfn)

    return spmv_pairs, mfunc_left, rfunc_left

def analyze_e2v_spmv(graph, rfunc):
    """Analyze if SPMV from edge space to node space can be applied.

    Parameters
    ----------
    graph: DGLGraph
        DGLGraph to use
    rfunc : list of dgl.function.BuiltinFunction
        The reduce function list.

    Returns
    -------
    spmv_rfunc : list
        A list of spmv-applicable reduce builtins.
    rfunc_left : list
        A list of reduce builtins that are not applicable
    """
    spmv_rfunc = []
    rfunc_left = []
    for rfn in rfunc:
        if rfn.is_spmv_supported():
            spmv_rfunc.append(rfn)
        else:
            rfunc_left.append(rfn)
    return spmv_rfunc, rfunc_left

def gen_v2v_spmv_schedule(adjmat, spmv_pairs, nf, ef, eid, out):
    """
    adjmat : sparse matrix
    spmv_pairs : list of pair
    nf : var.Var
        input node features
    ef : var.Var
        input edge features
    eid : var.Var
        eid index
    out : var.Var
        output node features
    """
    adj_var = var.SPMAT(adjmat)
    for mfn, rfn in spmv_pairs:
        #print('v2v mfn=%s rfn=%s' % (mfn.name, rfn.name))
        if mfn.use_edge_feature:
            ftedge = ir.READ(ef, eid, var.STR(mfn.edge_field))
            ftsrc = ir.READ_COL(nf, var.STR(mfn.src_field))
            ftdst = ir.SPMV_WITH_DATA(adj_var, ftedge, ftsrc)
        else:
            ftsrc = ir.READ_COL(nf, var.STR(mfn.src_field))
            ftdst = ir.SPMV(adj_var, ftsrc)
        # save for merge
        ir.WRITE_COL_(out, var.STR(rfn.out_field), ftdst)

def gen_e2v_spmv_schedule(inc, spmv_rfunc, mf, out):
    """
    inc : sparse matrix
        The incidence matrix
    spmv_rfunc : list of builtin reducers
    mf : var.Var
        Variable for message frame.
    out : var.Var
        Variable for output reduced features.
    """
    inc_var = var.SPMAT(inc)
    for rfn in spmv_rfunc:
        ftmsg = ir.READ_COL(mf, var.STR(rfn.msg_field))
        ftdst = ir.SPMV(inc_var, ftmsg)
        ir.WRITE_COL_(out, var.STR(rfn.out_field), ftdst)

def build_adj_matrix(call_type, graph, edges, reduce_nodes):
    """Build adjacency matrix.

    Parameters
    ----------
    call_type : str
        Can be 'update_all', 'send_and_recv'
    graph : DGLGraph
        The graph
    edges : tuple of utils.Index
        (u, v)
    reduce_nodes : utils.Index
        The nodes to reduce messages, which will be target dimension
        of the adjmat. The nodes include unique(v) and zero-degree-nodes.

    Returns
    -------
    utils.CtxCachedObject
        Get be used to get adjacency matrix on the provided ctx.
    """
    if call_type == "update_all":
        # full graph case
        return utils.CtxCachedObject(lambda ctx : graph.adjacency_matrix(ctx=ctx))
    elif call_type == "send_and_recv":
        # edgeset case
        mat = build_adj_matrix_uv(graph, edges, reduce_nodes)
        return utils.CtxCachedObject(lambda ctx : F.copy_to(mat, ctx))
    else:
        raise DGLError('Invalid call type:', call_type)

def build_adj_matrix_index_uv(graph, edges, reduce_nodes):
    """Build adj matrix index and shape using the given (u, v) edges.

    The matrix is of shape (len(reduce_nodes), n), where n is the number of nodes
    in the graph. Therefore, when doing SPMV, the src node data
    should be all the node features.

    The dst nodes will be sorted in the *unique-ascending* order of
    their ids. This is compatible with other reduce scheduler such as
    degree-bucketing scheduler.

    Paramters
    ---------
    graph : DGLGraph
        The graph
    edges : tuple of utils.Index
        (u, v)
    reduce_nodes : utils.Index
        The nodes to reduce messages, which will be target dimension
        of the adjmat. The nodes include unique(v) and zero-degree-nodes.

    Returns
    -------
    sparse index
        The sparse index.
    tupe of int
        The dense shape.
    """
    # TODO(minjie): add node frontier for this
    new2old, old2new = utils.build_relabel_map(reduce_nodes, sorted=True)
    u, v = edges
    u = u.tousertensor()
    v = v.tousertensor()
    new_v = old2new[v]  # FIXME(minjie): no use []
    n = graph.number_of_nodes()
    m = len(reduce_nodes)
    row = F.unsqueeze(new_v, 0)
    col = F.unsqueeze(u, 0)
    idx = F.cat([row, col], dim=0)
    return ('coo', idx), (m, n)

def build_adj_matrix_uv(graph, edges, reduce_nodes):
    """Build adj matrix using the given (u, v) edges and target nodes.

    The matrix is of shape (len(reduce_nodes), n), where n is the number of nodes
    in the graph. Therefore, when doing SPMV, the src node data
    should be all the node features.

    Paramters
    ---------
    graph : DGLGraph
        The graph
    edges : tuple of utils.Index
        (u, v)
    reduce_nodes : utils.Index
        The nodes to reduce messages, which will be target dimension
        of the adjmat. The nodes include unique(v) and zero-degree-nodes.

    Returns
    -------
    Sparse matrix
        The adjacency matrix on CPU
    """
    sp_idx, shape = build_adj_matrix_index_uv(graph, edges, reduce_nodes)
    u, v = edges
    nnz = len(u)
    # FIXME(minjie): data type
    dat = F.ones((nnz,), dtype=F.float32, ctx=F.cpu())
    mat = F.sparse_matrix(dat, sp_idx, shape)
    return mat

def build_inc_matrix(call_type, graph, dst, reduce_nodes):
    """Build incidence matrix.

    Parameters
    ----------
    call_type : str
        Can be 'update_all', 'send_and_recv'.
    graph : DGLGraph
        The graph.
    dst : utils.Index
        The destination nodes of the edges.
    reduce_nodes : utils.Index
        The nodes to reduce messages, which will be target dimension
        of the incmat. The nodes include unique(dst) and zero-degree-nodes.

    Returns
    -------
    utils.CtxCachedObject
        Get be used to get incidence matrix on the provided ctx.
    """
    if call_type == "update_all":
        # full graph case
        return utils.CtxCachedObject(lambda ctx : graph.incidence_matrix(type='in', ctx=ctx))
    elif call_type == "send_and_recv":
        # edgeset case
        mat = build_incmat_by_dst(dst, reduce_nodes)
        return utils.CtxCachedObject(lambda ctx : F.copy_to(mat, ctx))
    else:
        raise DGLError('Invalid call type:', call_type)

def build_incmat_by_eid(m, eid, dst, reduce_nodes):
    """Build incidence matrix using edge id and edge dst nodes.

    The incidence matrix is of shape (n, m), where n=len(reduce_nodes).
    The nnz is equal to len(eid).
    
    Invariant: len(eid) == len(dst)

    The dst nodes will be sorted in the *unique-ascending* order of
    their ids. This is compatible with other reduce scheduler such as
    degree-bucketing scheduler.

    Examples
    --------
    Total of seven edges. Three edges point to node 1 (eid=0,1,2);
    two point to node 3 (eid=3,4); two point to node 4 (eid=5,6).
    Only five edges should be included in the result incmat (eid=1,2,3,5,6).
    There are five nodes in the final target dimension (0~4),
    where node 0 and 2 are two 0-deg nodes.
    >>> m = 7
    >>> eid = [1, 2, 3, 5, 6]
    >>> dst = [1, 1, 3, 4, 4]
    >>> reduce_nodes = [0, 1, 2, 3, 4]
    >>> build_incmat_by_eid(m, eid, dst, reduce_nodes)
    tensor([[0, 0, 0, 0, 0, 0, 0],
            [0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 1]], shape=(5, 7))

    Paramters
    ---------
    m : int
        The source dimension size of the incidence matrix.
    eid : utils.Index
        The edge ids. All ids must be in range [0, m).
    dst : utils.Index
        The edge destination nodes. len(eid) == len(dst).
    reduce_nodes : utils.Index
        The nodes to reduce messages, which will be target dimension
        of the incmat. The nodes include unique(dst) and zero-degree-nodes.

    Returns
    -------
    Sparse matrix
        The incidence matrix on CPU
    """
    new2old, old2new = utils.build_relabel_map(reduce_nodes, sorted=True)
    dst = dst.tousertensor()
    eid = eid.tousertensor()
    # relabel edges dsts
    new_v = old2new[dst]  # FIXME(minjie): no use []
    # create sparse index tensor
    n = len(reduce_nodes)
    row = F.unsqueeze(new_v, 0)
    col = F.unsqueeze(eid, 0)
    idx = F.cat([row, col], dim=0)
    # create dat tensor
    nnz = len(eid)
    dat = F.ones((nnz,), dtype=F.float32, ctx=F.cpu())
    return F.sparse_matrix(dat, ('coo', idx), (n, m))

def build_incmat_by_dst(dst, reduce_nodes):
    """Build incidence matrix using only edge destinations.

    The incidence matrix is of shape (n, m), where n=len(reduce_nodes), m=len(dst).
    The nnz is equal to len(dst).

    Examples
    --------
    Five edges. Two edges point to node 1; one points to node 3;
    two point to node 4. There are five nodes in the final
    target dimension (0~4), where node 0 and 2 are two 0-deg nodes.
    >>> dst = [1, 1, 3, 4, 4]
    >>> reduce_nodes = [0, 1, 2, 3, 4]
    >>> build_incmat_by_dst(dst, reduced_nodes)
    tensor([[0, 0, 0, 0, 0],
            [1, 1, 0, 0, 0],
            [0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0],
            [0, 0, 0, 1, 1]], shape=(5, 5))
    
    Parameters
    ----------
    dst : utils.Index
        The edge destinations.
    reduce_nodes : utils.Index
        The nodes to reduce messages, which will be target dimension
        of the incmat. The nodes include unique(dst) and zero-degree-nodes.

    Returns
    -------
    Sparse matrix
        The incidence matrix on CPU
    """
    eid = utils.toindex(F.arange(0, len(dst)))
    return build_incmat_by_eid(len(eid), eid, dst, reduce_nodes)