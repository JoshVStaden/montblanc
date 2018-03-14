import unittest

import numpy as np
import tensorflow as tf
from tensorflow.python.client import device_lib


def np_create_antenna_jones(bsqrt, complex_phase, feed_rotation,
                                        ejones, arow_time_index):
    """ Compute antenna jones term using numpy """
    result = bsqrt[:,arow_time_index,:,:] * complex_phase[:,:,:,None]

    # Reshape npol dimensions to 2x2
    fr_shape = feed_rotation.shape[0:-1] + (2, 2)
    res_shape = result.shape[0:-1] + (2, 2)
    ej_shape = ejones.shape[0:-1] + (2, 2)

    # Multiple result into feed rotation
    # arow, i, j
    # src, arow, channel, j, k
    result = np.einsum("aij,sacjk->sacik",
                       feed_rotation.reshape(fr_shape),
                       result.reshape(res_shape))

    # Multiply result into ejones
    result = np.einsum("sacij,sacjk->sacik",
                       ejones.reshape(ej_shape),result)

    # Return shape in expected format
    return result.reshape(ejones.shape)


class TestCreateAntennaJones(unittest.TestCase):
    """ Tests the CreateAntennaJones operator """

    def setUp(self):
        # Load the rime operation library
        from montblanc.impl.rime.tensorflow import load_tf_lib
        self.rime = load_tf_lib()
        # Obtain a list of GPU device specifications ['/gpu:0', '/gpu:1', ...]
        self.gpu_devs = [d.name for d in device_lib.list_local_devices()
                                if d.device_type == 'GPU']

    def test_create_antenna_jones_operator(self):
        """ Tests the CreateAntennaJones operator """
        # List of type constraint for testing this operator
        type_permutations = [[np.float32, np.complex64],
                            [np.float64, np.complex128]]

        # Set up type permutation and jones term permutations
        # We don't test all jones term permutations because
        # the total output shape can't be inferred
        # for all combinations
        perms = []
        for type_perms in type_permutations:
            perms.append(type_perms + [True, True, False, False])
            perms.append(type_perms + [False, False, True, True])
            perms.append(type_perms + [False, False, False, True])

        # Run test with the type combinations above
        for FT, CT, bsqrt, cplx_phase, feed_rot, ddes in perms:
            self._impl_test_create_antenna_jones(FT, CT,
                                                bsqrt, cplx_phase,
                                                feed_rot, ddes)

    def _impl_test_create_antenna_jones(self, FT, CT,
                                        have_bsqrt, have_complex_phase,
                                        have_feed_rotation, have_ddes):
        """ Implementation of the CreateAntennaJones operator test """
        rf = lambda *s: np.random.random(size=s).astype(FT)
        rc = lambda *s: (rf(*s) + rf(*s) * 1j).astype(CT)

        nsrc, nchan, npol = 10, 16, 4

        ant_groups = np.random.randint(10, 20, size=15, dtype=np.int32)
        narow = ant_groups.sum()
        ntime = ant_groups.size
        time_index_range = np.arange(ntime, dtype=np.int32)
        arow_time_index = np.repeat(time_index_range, ant_groups)

        bsqrt = rc(nsrc, ntime, nchan, npol)
        complex_phase = rc(nsrc, narow, nchan)
        feed_rotation = rc(narow, npol)
        ejones = rc(nsrc, narow, nchan, npol)

        np_args = [bsqrt, complex_phase,
                   feed_rotation, ejones,
                   arow_time_index]
        arg_names = ["bsqrt", "complex_phase",
                     "feed_rotation", "ejones",
                     "arow_time_index"]

        tf_args = [tf.Variable(v, name=n) for v, n in zip(np_args, arg_names)]

        def _pin_op(device, *tf_args):
            """ Pin operation to device """
            with tf.device(device):
                return self.rime.create_antenna_jones(*tf_args, FT=FT,
                                have_bsqrt=have_bsqrt,
                                have_complex_phase=have_complex_phase,
                                have_feed_rotation=have_feed_rotation,
                                have_ddes=have_ddes)

        # Pin operation to CPU
        cpu_op = _pin_op('/cpu:0', *tf_args)

        # Run the op on all GPUs
        gpu_ops = [_pin_op(d, *tf_args) for d in self.gpu_devs]

        # Initialise variables
        init_op = tf.global_variables_initializer()

        with tf.Session() as S:
            S.run(init_op)

            # Get the CPU create_antenna_jones
            cpu_aj = S.run(cpu_op)

            # Only test against numpy if we have all the terms
            test_np = (have_bsqrt and have_complex_phase and
                        have_feed_rotation and have_ddes)

            if test_np:
                np_aj = np_create_antenna_jones(bsqrt, complex_phase,
                                                feed_rotation, ejones,
                                                arow_time_index)

                self.assertTrue(np.allclose(np_aj, cpu_aj))

            # Compare with GPU create_antenna_jones
            for gpu_aj in S.run(gpu_ops):
                self.assertTrue(np.allclose(cpu_aj, gpu_aj))

if __name__ == "__main__":
    unittest.main()
