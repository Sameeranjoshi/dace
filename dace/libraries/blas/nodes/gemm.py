from copy import deepcopy as dc
from typing import Any, Dict, Optional
from dace.data import Array
from dace.symbolic import symstr
from dace.properties import Property
import dace.library
import dace.graph.nodes
from dace.transformation.pattern_matching import ExpandTransformation
from dace.libraries.blas.blas_helpers import to_blastype, get_gemm_opts
from dace.libraries.blas.nodes.matmul import _get_matmul_inputs
from .. import environments


def _get_batchmm_opts(a_shape, a_strides, b_shape, b_strides, c_shape,
                      c_strides) -> Dict[str, Any]:
    """
    Detects whether a matrix multiplication is a batched matrix multiplication
    and returns its parameters (strides, batch size), or an empty dictionary if
    batched multiplication is not detected.
    :param a: Data descriptor for the first tensor.
    :param b: Data descriptor for the second tensor.
    :param c: Data descriptor for the output tensor (optional).
    :return: A dictionary with the following keys: sa,sb,sc (strides for a, b,
             and c); and b (batch size).
    """
    if len(a_shape) > 3 or len(b_shape) > 3 or (c_shape and len(c_shape) > 3):
        raise ValueError('Tensor dimensions too large for (batched) matrix '
                         'multiplication')
    if len(a_shape) <= 2 and len(b_shape) <= 2:
        return {}

    batch = None
    stride_a, stride_b, stride_c = 0, 0, 0
    if len(a_shape) == 3:
        batch = a_shape[0]
        stride_a = a_strides[0]
    if len(b_shape) == 3:
        if batch and batch != b_shape[0]:
            raise ValueError('Batch size mismatch for matrix multiplication')
        batch = b_shape[0]
        stride_b = b_strides[0]
    if c_shape and len(c_shape) == 3:
        if batch and batch != c_shape[0]:
            raise ValueError('Batch size mismatch for matrix multiplication')
        batch = c_shape[0]
        stride_c = c_strides[0]

    if batch is None:
        return {}

    return {'sa': stride_a, 'sb': stride_b, 'sc': stride_c, 'b': batch}


def _get_codegen_gemm_opts(node, state, sdfg, adesc, bdesc, cdesc, alpha, beta,
                           cdtype, func) -> Dict[str, Any]:
    """ Get option map for GEMM code generation (with column-major order). """
    # Avoid import loops
    from dace.codegen.targets.common import sym2cpp

    (_, _, ashape, astride), (_, _, bshape,
                              bstride) = _get_matmul_inputs(node, state, sdfg)
    opt = get_gemm_opts(astride, bstride, cdesc.strides)
    bopt = _get_batchmm_opts(ashape, astride, bshape, bstride, cdesc.shape,
                             cdesc.strides)
    opt['x'] = '_a'
    opt['y'] = '_b'
    opt['M'] = sym2cpp(ashape[-2])
    opt['N'] = sym2cpp(bshape[-1])
    opt['K'] = sym2cpp(ashape[-1])
    opt['lda'] = sym2cpp(opt['lda'])
    opt['ldb'] = sym2cpp(opt['ldb'])
    opt['ldc'] = sym2cpp(opt['ldc'])

    if opt['swap']:
        if bopt:
            bopt['sa'], bopt['sb'] = bopt['sb'], bopt['sa']
        opt['lda'], opt['ldb'] = opt['ldb'], opt['lda']
        opt['x'], opt['y'] = opt['y'], opt['x']
        opt['ta'], opt['tb'] = opt['tb'], opt['ta']
        opt['M'], opt['N'] = opt['N'], opt['M']

    opt['alpha'] = alpha
    opt['beta'] = beta
    opt['dtype'] = cdtype
    opt['func'] = func
    if bopt:
        opt['stride_a'] = sym2cpp(bopt['sa'])
        opt['stride_b'] = sym2cpp(bopt['sb'])
        opt['stride_c'] = sym2cpp(bopt['sc'])
        opt['BATCH'] = sym2cpp(bopt['b'])
    else:
        opt['BATCH'] = None

    return opt


def _is_complex(dtype):
    if hasattr(dtype, "is_complex"):
        return dtype.is_complex()
    else:
        return dtype in [np.complex64, np.complex128]


def _cast_to_dtype_str(value, dtype: dace.dtypes.typeclass) -> str:
    if _is_complex(dtype) and _is_complex(type(value)):
        raise ValueError("Cannot use complex beta with non-complex array")

    if _is_complex(dtype):
        cast_value = complex(value)

        return "dace.{type}({real}, {imag})".format(
            type=dace.DTYPE_TO_TYPECLASS[dtype].to_string(),
            real=cast_value.real,
            imag=cast_value.imag,
        )
    else:
        return "dace.{}({})".format(dace.DTYPE_TO_TYPECLASS[dtype].to_string(),
                                    value)


@dace.library.expansion
class ExpandGemmPure(ExpandTransformation):

    environments = []

    @staticmethod
    def make_sdfg(node, parent_state, parent_sdfg):
        sdfg = dace.SDFG(node.label + "_sdfg")

        ((edge_a, outer_array_a, shape_a), (edge_b, outer_array_b, shape_b),
         c_inputs) = _get_matmul_inputs(node, parent_state, parent_sdfg)

        dtype_a = outer_array_a.dtype.type
        dtype_b = outer_array_b.dtype.type
        dtype_y = dace.DTYPE_TO_TYPECLASS[np.result_type(dtype_a,
                                                         dtype_b).type]
        if c_inputs is not None:
            edge_c, outer_array_c, shape_c = c_inputs
            dtype_c = outer_array_c.dtype.type

        if node.transA:
            trans_shape_a = list(reversed(shape_a))
        else:
            trans_shape_a = shape_a

        if node.transB:
            trans_shape_b = list(reversed(shape_b))
        else:
            trans_shape_b = shape_b

        if (len(trans_shape_a) != 2 or len(trans_shape_b) != 2
                or trans_shape_a[1] != trans_shape_b[0]):
            raise SyntaxError("Matrix sizes must match")
        M, K, N = trans_shape_a[0], trans_shape_a[1], trans_shape_b[1]
        shape_y = (M, N)

        if outer_array_a.storage != outer_array_b.storage:
            raise ValueError("Input matrices must have same storage")
        storage = outer_array_a.storage

        _, array_a = sdfg.add_array("_a", shape_a, dtype_a, storage=storage)
        _, array_b = sdfg.add_array("_b", shape_b, dtype_b, storage=storage)
        if c_inputs is not None:
            _, array_c = sdfg.add_array("_c",
                                        shape_c,
                                        dtype_c,
                                        storage=storage)
        _, array_y = sdfg.add_array("_y", shape_y, dtype_y, storage=storage)

        if node.alpha == 1.0:
            mul_program = "__out = __a * __b"
        else:
            mul_program = "__out = {} * __a * __b".format(
                _cast_to_dtype_str(node.alpha, dtype_a))

        init_state = sdfg.add_state(node.label + "_initstate")
        state = sdfg.add_state_after(init_state, node.label + "_state")

        if c_inputs is None or node.beta == 0:
            mul_out, mul_out_array = "_y", array_y
            output_nodes = None
        else:
            mul_out, mul_out_array = tmp, array_tmp = sdfg.add_temp_transient(
                shape_y, dtype_y, storage=storage)

            access_tmp = state.add_read(tmp)
            output_nodes = {mul_out: access_tmp}

        # Initialization map
        init_state.add_mapped_tasklet(
            'gemm_init',
            {'_o%d' % i: '0:%s' % symstr(d)
             for i, d in enumerate(shape_y)}, {},
            'out = 0', {
                'out':
                dace.Memlet.simple(
                    mul_out, ','.join(
                        ['_o%d' % i for i in range(len(shape_y))]))
            },
            external_edges=True)

        # Multiplication map
        state.add_mapped_tasklet(
            "_GEMM_",
            {"__i%d" % i: "0:%s" % s
             for i, s in enumerate([M, N, K])}, {
                 "__a":
                 dace.Memlet.simple(
                     "_a", "__i2, __i0" if node.transA else "__i0, __i2"),
                 "__b":
                 dace.Memlet.simple(
                     "_b", "__i1, __i2" if node.transB else "__i2, __i1")
             },
            mul_program, {
                "__out":
                dace.Memlet.simple(
                    mul_out, "__i0, __i1", wcr_str="lambda x, y: x + y")
            },
            external_edges=True,
            output_nodes=output_nodes)

        if c_inputs is not None and node.beta != 0:
            add_program = "__y = ({} * __c) + __tmp".format(
                _cast_to_dtype_str(node.beta, dtype_a))

            # manually broadcasting C to [M, N]
            if shape_c == [M, N]:
                memlet_idx = '__i0, __i1'
            elif shape_c == [1, N]:
                memlet_idx = '0, __i1'
            elif shape_c == [M, 1]:
                memlet_idx = '__i0, 0'
            elif shape_c == [
                    N,
            ]:
                memlet_idx = '__i1'
            else:
                raise ValueError(
                    "Could not broadcast input _c to ({}, {})".format(M, N))

            # addition map
            state.add_mapped_tasklet(
                "_Add_",
                {"__i%d" % i: "0:%s" % s
                 for i, s in enumerate([M, N])}, {
                     "__c": dace.Memlet.simple("_c", memlet_idx),
                     "__tmp": dace.Memlet.simple(mul_out, "__i0, __i1"),
                 },
                add_program, {"__y": dace.Memlet.simple("_y", "__i0, __i1")},
                external_edges=True,
                input_nodes={mul_out: access_tmp})

            sdfg.parent = parent_sdfg
        sdfg.parent_sdfg = parent_sdfg

        return sdfg

    @staticmethod
    def expansion(node, state, sdfg):
        node.validate(sdfg, state)
        if node.dtype is None:
            raise ValueError("Data type must be set to expand " + str(node) +
                             ".")
        return ExpandGemmPure.make_sdfg(node, state, sdfg)


@dace.library.expansion
class ExpandGemmMKL(ExpandTransformation):

    environments = [environments.intel_mkl.IntelMKL]

    @staticmethod
    def expansion(node, state, sdfg):
        node.validate(sdfg, state)
        dtype = node.dtype
        func = to_blastype(dtype.type).lower() + 'gemm'
        if dtype == dace.float32:
            alpha = "1.0f"
            beta = "0.0f"
        elif dtype == dace.float64:
            alpha = "1.0"
            beta = "0.0"
        elif dtype == dace.complex64:
            alpha = "dace::blas::BlasConstants::Get().Complex64Pone()"
            beta = "dace::blas::BlasConstants::Get().Complex64Zero()"
        elif dtype == dace.complex128:
            alpha = "dace::blas::BlasConstants::Get().Complex128Pone()"
            beta = "dace::blas::BlasConstants::Get().Complex128Zero()"
        else:
            raise ValueError("Unsupported type for BLAS dot product: " +
                             str(dtype))
        (_, adesc, ashape,
         astrides), (_, bdesc, bshape,
                     bstrides) = _get_matmul_inputs(node, state, sdfg)
        cdesc = sdfg.arrays[state.out_edges(node)[0].data.data]
        opt = _get_codegen_gemm_opts(node, state, sdfg, adesc, bdesc, cdesc,
                                     alpha, beta, cdesc.dtype.ctype, func)

        # Adaptations for MKL/BLAS API
        opt['ta'] = 'CblasNoTrans' if opt['ta'] == 'N' else 'CblasTrans'
        opt['tb'] = 'CblasNoTrans' if opt['tb'] == 'N' else 'CblasTrans'

        if not opt['BATCH']:
            code = ("cblas_{func}(CblasColMajor, {ta}, {tb}, "
                    "{M}, {N}, {K}, {alpha}, {x}, {lda}, {y}, {ldb}, {beta}, "
                    "_c, {ldc});").format_map(opt)
        else:
            code = '''
            for (int __ib = 0; __ib < {BATCH}; ++__ib) {{
                cblas_{func}(CblasColMajor, {ta}, {tb}, {M}, {N}, {K}, {alpha},
                             (({dtype}*){x}) + __ib*{stride_a}, {lda},
                             (({dtype}*){y}) + __ib*{stride_b}, {ldb},
                             {beta},
                             (({dtype}*)_c) + __ib*{stride_c}, {ldc});
            }}'''.format_map(opt)

        tasklet = dace.graph.nodes.Tasklet(node.name,
                                           node.in_connectors,
                                           node.out_connectors,
                                           code,
                                           language=dace.dtypes.Language.CPP)
        return tasklet


@dace.library.expansion
class ExpandGemmCuBLAS(ExpandTransformation):

    environments = [environments.cublas.cuBLAS]

    @staticmethod
    def expansion(node, state, sdfg):
        node.validate(sdfg, state)
        dtype = node.dtype
        func = '%sgemm' % to_blastype(dtype.type)
        if dtype == dace.float16:
            cdtype = '__half'
            factort = 'Half'
        elif dtype == dace.float32:
            cdtype = 'float'
            factort = 'Float'
        elif dtype == dace.float64:
            cdtype = 'double'
            factort = 'Double'
        elif dtype == dace.complex64:
            cdtype = 'cuComplex'
            factort = 'Complex64'
        elif dtype == dace.complex128:
            cdtype = 'cuDoubleComplex'
            factort = 'Complex128'
        else:
            raise ValueError("Unsupported type: " + str(dtype))

        alpha = "dace::blas::CublasConstants::Get(__dace_cuda_device).%sPone()" % factort
        beta = "dace::blas::CublasConstants::Get(__dace_cuda_device).%sZero()" % factort

        # Find inputs and output
        adesc, bdesc, cdesc = None, None, None
        for e in state.in_edges(node):
            if e.dst_conn == '_a':
                anode = state.memlet_path(e)[0].src
                if isinstance(anode, dace.graph.nodes.AccessNode):
                    adesc: Array = sdfg.arrays[anode.data]
            elif e.dst_conn == '_b':
                bnode = state.memlet_path(e)[0].src
                if isinstance(bnode, dace.graph.nodes.AccessNode):
                    bdesc: Array = sdfg.arrays[bnode.data]
        for e in state.out_edges(node):
            if e.src_conn == '_c':
                cnode = state.memlet_path(e)[-1].dst
                if isinstance(cnode, dace.graph.nodes.AccessNode):
                    cdesc: Array = sdfg.arrays[cnode.data]
        if not adesc or not bdesc or not cdesc:
            raise ValueError('Unsupported input/output arrays')

        # Set up options for code formatting
        opt = _get_codegen_gemm_opts(node, state, sdfg, adesc, bdesc, cdesc,
                                     alpha, beta, cdtype, func)

        # Matrix multiplication
        if not opt['BATCH']:
            call = '''cublas{func}(__dace_cublas_handle,
                CUBLAS_OP_{ta}, CUBLAS_OP_{tb},
                {M}, {N}, {K},
                {alpha},
                ({dtype}*){x}, {lda},
                ({dtype}*){y}, {ldb},
                {beta},
                ({dtype}*)_c, {ldc});'''
        else:  # Batched matrix multiplication
            call = '''cublas{func}StridedBatched(__dace_cublas_handle,
                CUBLAS_OP_{ta}, CUBLAS_OP_{tb},
                {M}, {N}, {K},
                {alpha},
                ({dtype}*){x}, {lda}, {stride_a},
                ({dtype}*){y}, {ldb}, {stride_b},
                {beta},
                ({dtype}*)_c, {ldc}, {stride_c},
                {BATCH});'''

        code = (environments.cublas.cuBLAS.handle_setup_code(node) +
                call.format_map(opt))
        tasklet = dace.graph.nodes.Tasklet(node.name,
                                           node.in_connectors,
                                           node.out_connectors,
                                           code,
                                           language=dace.dtypes.Language.CPP)

        # If buffers are not on the GPU, copy them
        # TODO: This creates variable shadowing
        if any(desc.storage not in
               [dace.StorageType.GPU_Global, dace.StorageType.CPU_Pinned]
               for desc in [adesc, bdesc, cdesc]):
            nsdfg = dace.SDFG('nested_gemm')
            for name, desc in [('_a', adesc), ('_b', bdesc), ('_c', cdesc)]:
                dcopy = dc(desc)
                dcopy.transient = False
                nsdfg.add_datadesc(name, dcopy)
                dcopy_gpu = dc(desc)
                dcopy_gpu.transient = True
                dcopy_gpu.storage = dace.StorageType.GPU_Global
                nsdfg.add_datadesc(name + '_gpu', dcopy_gpu)
            nstate = nsdfg.add_state()
            a = nstate.add_read('_a')
            ga = nstate.add_access('_a_gpu')
            b = nstate.add_read('_b')
            gb = nstate.add_access('_b_gpu')
            c = nstate.add_write('_c')
            gc = nstate.add_access('_c_gpu')
            nstate.add_node(tasklet)
            nstate.add_nedge(a, ga, dace.Memlet.from_array('_a', adesc))
            nstate.add_nedge(b, gb, dace.Memlet.from_array('_b', bdesc))
            nstate.add_edge(ga, None, tasklet, '_a',
                            dace.Memlet.from_array('_a_gpu', adesc))
            nstate.add_edge(gb, None, tasklet, '_b',
                            dace.Memlet.from_array('_b_gpu', bdesc))
            nstate.add_edge(tasklet, '_c', gc, None,
                            dace.Memlet.from_array('_c_gpu', cdesc))
            nstate.add_nedge(gc, c, dace.Memlet.from_array('_c', cdesc))

            return nsdfg
        # End of copy to GPU

        return tasklet


@dace.library.node
class Gemm(dace.graph.nodes.LibraryNode):

    # Global properties
    implementations = {
        "pure": ExpandGemmPure,
        "MKL": ExpandGemmMKL,
        "cuBLAS": ExpandGemmCuBLAS
    }
    default_implementation = None

    # Object fields
    dtype = dace.properties.TypeClassProperty(allow_none=True)
    transA = Property(dtype=bool,
                      desc="Whether to transpose A before multiplying")
    transB = Property(dtype=bool,
                      desc="Whether to transpose B before multiplying")
    alpha = Property(
        dtype=tuple(dace.dtypes._CONSTANT_TYPES),
        default=1,
        desc="A scalar which will be multiplied with A @ B before adding C")
    beta = Property(
        dtype=tuple(dace.dtypes._CONSTANT_TYPES),
        default=1,
        desc="A scalar which will be multiplied with C before adding C")

    def __init__(self,
                 name,
                 dtype=None,
                 location=None,
                 transA=False,
                 transB=False,
                 alpha=1,
                 beta=0):
        super().__init__(name,
                         location=location,
                         inputs={"_a", "_b", "_c"},
                         outputs={"_y"})
        self.dtype = dtype
        self.transA = transA
        self.transB = transB
        self.alpha = alpha
        self.beta = beta

    def validate(self, sdfg, state):
        in_edges = state.in_edges(self)
        if len(in_edges) not in [2, 3]:
            raise ValueError("Expected 2 or 3 inputs to gemm")
        size2 = None
        for _, _, _, dst_conn, memlet in state.in_edges(self):
            if dst_conn == '_a':
                subset = dc(memlet.subset)
                subset.squeeze()
                size0 = subset.size()
            if dst_conn == '_b':
                subset = dc(memlet.subset)
                subset.squeeze()
                size1 = subset.size()
            if dst_conn == '_c':
                subset = dc(memlet.subset)
                subset.squeeze()
                size2 = subset.size()
        out_edges = state.out_edges(self)
        if len(out_edges) != 1:
            raise ValueError(
                "Expected exactly one output from matrix-matrix product")
        out_memlet = out_edges[0].data
        # Function is symmetric, edge order does not matter
        if len(size0) != 2 or len(size1) != 2 or len(size2) != 2:
            raise ValueError(
                "matrix-matrix product only supported on matrices")
        if size0[1] != size1[0]:
            raise ValueError("Inputs to matrix-matrix product "
                             "must agree in the k-dimension")
        out_subset = dc(out_memlet.subset)
        out_subset.squeeze()
        size3 = out_subset.size()
        if len(size3) != 2:
            raise ValueError(
                "matrix-matrix product only supported on matrices")
        if len(size3) == 2 and list(size3) != [size0[-2], size1[-1]]:
            raise ValueError(
                "Output to matrix-matrix product must agree in the m and n "
                "dimensions")
        if size2 is not None and size2 != size3:
            raise ValueError("Sizes of input and output C-matrix do not match")
