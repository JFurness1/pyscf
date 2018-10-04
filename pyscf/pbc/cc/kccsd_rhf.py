#!/usr/bin/env python
# Copyright 2014-2018 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors: James D. McClain
#          Timothy Berkelbach <tim.berkelbach@gmail.com>
#

import time
from functools import reduce
import numpy as np
import h5py

from pyscf import lib
import pyscf.ao2mo
from pyscf.lib import logger
import pyscf.cc
import pyscf.cc.ccsd
from pyscf.pbc import scf
from pyscf.pbc.mp.kmp2 import (get_frozen_mask, get_nocc, get_nmo,
                               padded_mo_coeff, padding_k_idx_mp as padding_k_idx)
from pyscf.pbc.cc import kintermediates_rhf as imdk
from pyscf.pbc.cc.eom_kccsd_rhf_ip import kernel as kernel_ip
from pyscf.pbc.cc.eom_kccsd_rhf_ea import kernel as kernel_ea
from pyscf.lib.parameters import LOOSE_ZERO_TOL, LARGE_DENOM
from pyscf.lib import linalg_helper
from pyscf.pbc.lib import kpts_helper
from pyscf.pbc.lib.kpts_helper import member, gamma_point, VectorComposer, VectorSplitter
from pyscf import __config__

# einsum = np.einsum
einsum = lib.einsum

# This is restricted (R)CCSD
# Ref: Hirata, et al., J. Chem. Phys. 120, 2581 (2004)

kernel = pyscf.cc.ccsd.kernel


def get_normt_diff(cc, t1, t2, t1new, t2new):
    '''Calculates norm(t1 - t1new) + norm(t2 - t2new).'''
    return np.linalg.norm(t1new - t1) + np.linalg.norm(t2new - t2)


def update_amps(cc, t1, t2, eris):
    time0 = time1 = time.clock(), time.time()
    log = logger.Logger(cc.stdout, cc.verbose)
    nkpts, nocc, nvir = t1.shape
    fock = eris.fock
    mo_e_o = [e[:nocc] for e in eris.mo_energy]
    mo_e_v = [e[nocc:] + cc.level_shift for e in eris.mo_energy]

    # Get location of padded elements in occupied and virtual space
    nonzero_opadding, nonzero_vpadding = padding_k_idx(cc, kind="split")

    fov = fock[:, :nocc, nocc:]
    foo = fock[:, :nocc, :nocc]
    fvv = fock[:, nocc:, nocc:]

    kconserv = cc.khelper.kconserv

    Foo = imdk.cc_Foo(t1, t2, eris, kconserv)
    Fvv = imdk.cc_Fvv(t1, t2, eris, kconserv)
    Fov = imdk.cc_Fov(t1, t2, eris, kconserv)
    Loo = imdk.Loo(t1, t2, eris, kconserv)
    Lvv = imdk.Lvv(t1, t2, eris, kconserv)

    # Move energy terms to the other side
    for k in range(nkpts):
        Foo[k][np.diag_indices(nocc)] -= mo_e_o[k]
        Fvv[k][np.diag_indices(nvir)] -= mo_e_v[k]
        Loo[k][np.diag_indices(nocc)] -= mo_e_o[k]
        Lvv[k][np.diag_indices(nvir)] -= mo_e_v[k]
    time1 = log.timer_debug1('intermediates', *time1)

    # T1 equation
    t1new = np.array(fov).astype(t1.dtype).conj()

    for ka in range(nkpts):
        ki = ka
        # kc == ki; kk == ka
        t1new[ka] += -2. * einsum('kc,ka,ic->ia', fov[ki], t1[ka], t1[ki])
        t1new[ka] += einsum('ac,ic->ia', Fvv[ka], t1[ki])
        t1new[ka] += -einsum('ki,ka->ia', Foo[ki], t1[ka])

        tau_term = np.empty((nkpts, nocc, nocc, nvir, nvir), dtype=t1.dtype)
        for kk in range(nkpts):
            tau_term[kk] = 2 * t2[kk, ki, kk] - t2[ki, kk, kk].transpose(1, 0, 2, 3)
        tau_term[ka] += einsum('ic,ka->kica', t1[ki], t1[ka])

        for kk in range(nkpts):
            kc = kk
            t1new[ka] += einsum('kc,kica->ia', Fov[kc], tau_term[kk])

            t1new[ka] += einsum('akic,kc->ia', 2 * eris.voov[ka, kk, ki], t1[kc])
            t1new[ka] += einsum('kaic,kc->ia', -eris.ovov[kk, ka, ki], t1[kc])

            for kc in range(nkpts):
                kd = kconserv[ka, kc, kk]

                Svovv = 2 * eris.vovv[ka, kk, kc] - eris.vovv[ka, kk, kd].transpose(0, 1, 3, 2)
                tau_term_1 = t2[ki, kk, kc].copy()
                if ki == kc and kk == kd:
                    tau_term_1 += einsum('ic,kd->ikcd', t1[ki], t1[kk])
                t1new[ka] += einsum('akcd,ikcd->ia', Svovv, tau_term_1)

                # kk - ki + kl = kc
                #  => kl = ki - kk + kc
                kl = kconserv[ki, kk, kc]
                Sooov = 2 * eris.ooov[kk, kl, ki] - eris.ooov[kl, kk, ki].transpose(1, 0, 2, 3)
                tau_term_1 = t2[kk, kl, ka].copy()
                if kk == ka and kl == kc:
                    tau_term_1 += einsum('ka,lc->klac', t1[ka], t1[kc])
                t1new[ka] += -einsum('klic,klac->ia', Sooov, tau_term_1)
    time1 = log.timer_debug1('t1', *time1)

    # T2 equation
    t2new = np.empty_like(t2)
    for ki, kj, ka in kpts_helper.loop_kkk(nkpts):
        t2new[ki, kj, ka] = eris.oovv[ki, kj, ka].conj()

    mem_now = lib.current_memory()[0]
    if (nocc ** 4 * nkpts ** 3) * 16 / 1e6 + mem_now < cc.max_memory * .9:
        Woooo = imdk.cc_Woooo(t1, t2, eris, kconserv)
    else:
        fimd = lib.H5TmpFile()
        Woooo = fimd.create_dataset('oooo', (nkpts, nkpts, nkpts, nocc, nocc, nocc, nocc), t1.dtype.char)
        Woooo = imdk.cc_Woooo(t1, t2, eris, kconserv, Woooo)

    for ki, kj, ka in kpts_helper.loop_kkk(nkpts):
        # Chemist's notation for momentum conserving t2(ki,kj,ka,kb)
        kb = kconserv[ki, ka, kj]

        t2new_tmp = np.zeros((nocc, nocc, nvir, nvir), dtype=t2.dtype)
        for kl in range(nkpts):
            kk = kconserv[kj, kl, ki]
            tau_term = t2[kk, kl, ka].copy()
            if kl == kb and kk == ka:
                tau_term += einsum('ic,jd->ijcd', t1[ka], t1[kb])
            t2new_tmp += 0.5 * einsum('klij,klab->ijab', Woooo[kk, kl, ki], tau_term)
        t2new[ki, kj, ka] += t2new_tmp
        t2new[kj, ki, kb] += t2new_tmp.transpose(1, 0, 3, 2)
    Woooo = None
    fimd = None
    time1 = log.timer_debug1('t2 oooo', *time1)

    # einsum('abcd,ijcd->ijab', Wvvvv, tau)
    add_vvvv_(cc, t2new, t1, t2, eris)
    time1 = log.timer_debug1('t2 vvvv', *time1)

    for ki, kj, ka in kpts_helper.loop_kkk(nkpts):
        kb = kconserv[ki, ka, kj]

        t2new_tmp = einsum('ac,ijcb->ijab', Lvv[ka], t2[ki, kj, ka])
        t2new_tmp += einsum('ki,kjab->ijab', -Loo[ki], t2[ki, kj, ka])

        kc = kconserv[ka, ki, kb]
        tmp2 = np.asarray(eris.vovv[kc, ki, kb]).transpose(3, 2, 1, 0).conj() \
               - einsum('kbic,ka->abic', eris.ovov[ka, kb, ki], t1[ka])
        t2new_tmp += einsum('abic,jc->ijab', tmp2, t1[kj])

        kk = kconserv[ki, ka, kj]
        tmp2 = np.asarray(eris.ooov[kj, ki, kk]).transpose(3, 2, 1, 0).conj() \
               + einsum('akic,jc->akij', eris.voov[ka, kk, ki], t1[kj])
        t2new_tmp -= einsum('akij,kb->ijab', tmp2, t1[kb])
        t2new[ki, kj, ka] += t2new_tmp
        t2new[kj, ki, kb] += t2new_tmp.transpose(1, 0, 3, 2)

    mem_now = lib.current_memory()[0]
    if (nocc ** 2 * nvir ** 2 * nkpts ** 3) * 16 / 1e6 * 2 + mem_now < cc.max_memory * .9:
        Wvoov = imdk.cc_Wvoov(t1, t2, eris, kconserv)
        Wvovo = imdk.cc_Wvovo(t1, t2, eris, kconserv)
    else:
        fimd = lib.H5TmpFile()
        Wvoov = fimd.create_dataset('voov', (nkpts, nkpts, nkpts, nvir, nocc, nocc, nvir), t1.dtype.char)
        Wvovo = fimd.create_dataset('vovo', (nkpts, nkpts, nkpts, nvir, nocc, nvir, nocc), t1.dtype.char)
        Wvoov = imdk.cc_Wvoov(t1, t2, eris, kconserv, Wvoov)
        Wvovo = imdk.cc_Wvovo(t1, t2, eris, kconserv, Wvovo)

    for ki, kj, ka in kpts_helper.loop_kkk(nkpts):
        kb = kconserv[ki, ka, kj]
        t2new_tmp = np.zeros((nocc, nocc, nvir, nvir), dtype=t2.dtype)
        for kk in range(nkpts):
            kc = kconserv[ka, ki, kk]
            tmp_voov = 2. * Wvoov[ka, kk, ki] - Wvovo[ka, kk, kc].transpose(0, 1, 3, 2)
            t2new_tmp += einsum('akic,kjcb->ijab', tmp_voov, t2[kk, kj, kc])

            kc = kconserv[ka, ki, kk]
            t2new_tmp -= einsum('akic,kjbc->ijab', Wvoov[ka, kk, ki], t2[kk, kj, kb])

            kc = kconserv[kk, ka, kj]
            t2new_tmp -= einsum('bkci,kjac->ijab', Wvovo[kb, kk, kc], t2[kk, kj, ka])

        t2new[ki, kj, ka] += t2new_tmp
        t2new[kj, ki, kb] += t2new_tmp.transpose(1, 0, 3, 2)
    Wvoov = Wvovo = None
    fimd = None
    time1 = log.timer_debug1('t2 voov', *time1)

    for ki in range(nkpts):
        ka = ki
        # Remove zero/padded elements from denominator
        eia = LARGE_DENOM * np.ones((nocc, nvir), dtype=eris.mo_energy[0].dtype)
        n0_ovp_ia = np.ix_(nonzero_opadding[ki], nonzero_vpadding[ka])
        eia[n0_ovp_ia] = (mo_e_o[ki][:,None] - mo_e_v[ka])[n0_ovp_ia]
        t1new[ki] /= eia

    for ki, kj, ka in kpts_helper.loop_kkk(nkpts):
        kb = kconserv[ki, ka, kj]
        # For LARGE_DENOM, see t1new update above
        eia = LARGE_DENOM * np.ones((nocc, nvir), dtype=eris.mo_energy[0].dtype)
        n0_ovp_ia = np.ix_(nonzero_opadding[ki], nonzero_vpadding[ka])
        eia[n0_ovp_ia] = (mo_e_o[ki][:,None] - mo_e_v[ka])[n0_ovp_ia]

        ejb = LARGE_DENOM * np.ones((nocc, nvir), dtype=eris.mo_energy[0].dtype)
        n0_ovp_jb = np.ix_(nonzero_opadding[kj], nonzero_vpadding[kb])
        ejb[n0_ovp_jb] = (mo_e_o[kj][:,None] - mo_e_v[kb])[n0_ovp_jb]
        eijab = eia[:, None, :, None] + ejb[:, None, :]

        t2new[ki, kj, ka] /= eijab

    time0 = log.timer_debug1('update t1 t2', *time0)

    return t1new, t2new


def energy(cc, t1, t2, eris):
    nkpts, nocc, nvir = t1.shape
    kconserv = cc.khelper.kconserv
    fock = eris.fock
    e = 0.0 + 1j * 0.0
    for ki in range(nkpts):
        e += 2 * einsum('ia,ia', fock[ki, :nocc, nocc:], t1[ki])
    tau = t1t1 = np.zeros(shape=t2.shape, dtype=t2.dtype)
    for ki in range(nkpts):
        ka = ki
        for kj in range(nkpts):
            # kb = kj
            t1t1[ki, kj, ka] = einsum('ia,jb->ijab', t1[ki], t1[kj])
    tau += t2
    for ki in range(nkpts):
        for kj in range(nkpts):
            for ka in range(nkpts):
                kb = kconserv[ki, ka, kj]
                e += 2 * einsum('ijab,ijab', tau[ki, kj, ka], eris.oovv[ki, kj, ka])
                e += -einsum('ijab,ijba', tau[ki, kj, ka], eris.oovv[ki, kj, kb])
    e /= nkpts
    if abs(e.imag) > 1e-4:
        logger.warn(cc, 'Non-zero imaginary part found in KRCCSD energy %s', e)
    return e.real


def add_vvvv_(cc, Ht2, t1, t2, eris):
    nocc = cc.nocc
    nmo = cc.nmo
    nvir = nmo - nocc
    nkpts = cc.nkpts
    kconserv = cc.khelper.kconserv

    mem_now = lib.current_memory()[0]
    if cc.direct and hasattr(eris, 'Lpv'):
        #: If memory is not enough to hold eris.Lpv
        #:def get_Wvvvv(ka, kb, kc):
        #:    kd = kconserv[ka,kc,kb]
        #:    v = cc._scf.with_df.ao2mo([eris.mo_coeff[k] for k in [ka,kc,kb,kd]],
        #:                              cc.kpts[[ka,kc,kb,kd]]).reshape([nmo]*4)
        #:    Wvvvv  = lib.einsum('kcbd,ka->abcd', v[:nocc,nocc:,nocc:,nocc:], -t1[ka])
        #:    Wvvvv += lib.einsum('ackd,kb->abcd', v[nocc:,nocc:,:nocc,nocc:], -t1[kb])
        #:    Wvvvv += v[nocc:,nocc:,nocc:,nocc:].transpose(0,2,1,3)
        #:    Wvvvv *= (1./nkpts)
        #:    return Wvvvv
        def get_Wvvvv(ka, kb, kc):
            kd = kconserv[ka, kc, kb]
            Lbd = (eris.Lpv[kb, kd, :, nocc:] -
                   lib.einsum('Lkd,kb->Lbd', eris.Lpv[kb, kd, :, :nocc], t1[kb]))
            Wvvvv = lib.einsum('Lac,Lbd->abcd', eris.Lpv[ka, kc, :, nocc:], Lbd)
            Lbd = None
            kcbd = lib.einsum('Lkc,Lbd->kcbd', eris.Lpv[ka, kc, :, :nocc],
                              eris.Lpv[kb, kd, :, nocc:])
            Wvvvv -= lib.einsum('kcbd,ka->abcd', kcbd, t1[ka])
            Wvvvv *= (1. / nkpts)
            return Wvvvv

    elif (nvir ** 4 * nkpts ** 3) * 16 / 1e6 + mem_now < cc.max_memory * .9:
        _Wvvvv = imdk.cc_Wvvvv(t1, t2, eris, kconserv)

        def get_Wvvvv(ka, kb, kc):
            return _Wvvvv[ka, kb, kc]
    else:
        fimd = lib.H5TmpFile()
        _Wvvvv = fimd.create_dataset('vvvv', (nkpts, nkpts, nkpts, nvir, nvir, nvir, nvir), t1.dtype.char)
        _Wvvvv = imdk.cc_Wvvvv(t1, t2, eris, kconserv, _Wvvvv)

        def get_Wvvvv(ka, kb, kc):
            return _Wvvvv[ka, kb, kc]

    #:Ps = kconserve_pmatrix(cc.nkpts, cc.khelper.kconserv)
    #:Wvvvv = einsum('xyzakcd,ykb->xyzabcd', eris.vovv, -t1)
    #:Wvvvv = Wvvvv + einsum('xyzabcd,xyzw->yxwbadc', Wvvvv, Ps)
    #:Wvvvv += eris.vvvv
    #:
    #:tau = t2.copy()
    #:idx = np.arange(nkpts)
    #:tau[idx,:,idx] += einsum('xic,yjd->xyijcd', t1, t1)
    #:Ht2 += einsum('xyuijcd,zwuabcd,xyuv,zwuv->xyzijab', tau, Wvvvv, Ps, Ps)
    for ka, kb, kc in kpts_helper.loop_kkk(nkpts):
        kd = kconserv[ka, kc, kb]
        Wvvvv = get_Wvvvv(ka, kb, kc)
        for ki in range(nkpts):
            kj = kconserv[ka, ki, kb]
            tau = t2[ki, kj, kc].copy()
            if ki == kc and kj == kd:
                tau += np.einsum('ic,jd->ijcd', t1[ki], t1[kj])
            Ht2[ki, kj, ka] += lib.einsum('abcd,ijcd->ijab', Wvvvv, tau)
    fimd = None
    return Ht2


# Ps is Permutation transformation matrix
# The physical meaning of Ps matrix is the conservation of moment.
# Given the four indices in Ps, the element shows whether moment conservation
# holds (1) or not (0)
def kconserve_pmatrix(nkpts, kconserv):
    Ps = np.zeros((nkpts, nkpts, nkpts, nkpts))
    for ki in range(nkpts):
        for kj in range(nkpts):
            for ka in range(nkpts):
                # Chemist's notation for momentum conserving t2(ki,kj,ka,kb)
                kb = kconserv[ki, ka, kj]
                Ps[ki, kj, ka, kb] = 1
    return Ps


class RCCSD(pyscf.cc.ccsd.CCSD):
    max_space = getattr(__config__, 'pbc_cc_kccsd_rhf_KRCCSD_max_space', 20)

    def __init__(self, mf, frozen=0, mo_coeff=None, mo_occ=None):
        assert (isinstance(mf, scf.khf.KSCF))
        pyscf.cc.ccsd.CCSD.__init__(self, mf, frozen, mo_coeff, mo_occ)
        self.kpts = mf.kpts
        self.khelper = kpts_helper.KptsHelper(mf.cell, mf.kpts)
        self.made_ee_imds = False
        self.made_ip_imds = False
        self.made_ea_imds = False
        self.ip_partition = None
        self.ea_partition = None
        self.direct = True  # If possible, use GDF to compute Wvvvv on-the-fly

        keys = set(['kpts', 'khelper', 'made_ee_imds',
                    'made_ip_imds', 'made_ea_imds', 'ip_partition',
                    'ea_partition', 'max_space', 'direct'])
        self._keys = self._keys.union(keys)
        self.__imds__ = None

    @property
    def nkpts(self):
        return len(self.kpts)

    get_normt_diff = get_normt_diff
    get_nocc = get_nocc
    get_nmo = get_nmo
    get_frozen_mask = get_frozen_mask

    def dump_flags(self):
        return pyscf.cc.ccsd.CCSD.dump_flags(self)

    def amplitudes_to_vector(self, t1, t2):
        """Ground state amplitudes to a vector."""
        vc = VectorComposer(t1.dtype)
        vc.put(t1)
        vc.put(t2)
        return vc.flush()

    def vector_to_amplitudes(self, vec):
        """Ground state vector to apmplitudes."""
        vs = VectorSplitter(vec)
        nvir = self.nmo - self.nocc
        return vs.get((self.nkpts, self.nocc, nvir)), vs.get((self.nkpts,) * 3 + (self.nocc,) * 2 + (nvir,) * 2)

    def init_amps(self, eris):
        time0 = time.clock(), time.time()
        nocc = self.nocc
        nvir = self.nmo - nocc
        nkpts = self.nkpts
        t1 = np.zeros((nkpts,nocc,nvir), dtype=eris.fock.dtype)
        t2 = np.empty((nkpts,nkpts,nkpts,nocc,nocc,nvir,nvir), dtype=eris.fock.dtype)
        mo_e_o = [eris.mo_energy[k][:nocc] for k in range(nkpts)]
        mo_e_v = [eris.mo_energy[k][nocc:] for k in range(nkpts)]

        # Get location of padded elements in occupied and virtual space
        nonzero_opadding, nonzero_vpadding = padding_k_idx(self, kind="split")

        emp2 = 0
        kconserv = self.khelper.kconserv
        touched = np.zeros((nkpts, nkpts, nkpts), dtype=bool)
        for ki, kj, ka in kpts_helper.loop_kkk(nkpts):
            if touched[ki, kj, ka]:
                continue

            kb = kconserv[ki, ka, kj]
            # For discussion of LARGE_DENOM, see t1new update above
            eia = LARGE_DENOM * np.ones((nocc, nvir), dtype=eris.mo_energy[0].dtype)
            n0_ovp_ia = np.ix_(nonzero_opadding[ki], nonzero_vpadding[ka])
            eia[n0_ovp_ia] = (mo_e_o[ki][:,None] - mo_e_v[ka])[n0_ovp_ia]

            ejb = LARGE_DENOM * np.ones((nocc, nvir), dtype=eris.mo_energy[0].dtype)
            n0_ovp_jb = np.ix_(nonzero_opadding[kj], nonzero_vpadding[kb])
            ejb[n0_ovp_jb] = (mo_e_o[kj][:,None] - mo_e_v[kb])[n0_ovp_jb]
            eijab = eia[:, None, :, None] + ejb[:, None, :]

            eris_ijab = eris.oovv[ki, kj, ka]
            eris_ijba = eris.oovv[ki, kj, kb]
            t2[ki, kj, ka] = eris_ijab.conj() / eijab
            woovv = 2 * eris_ijab - eris_ijba.transpose(0, 1, 3, 2)
            emp2 += np.einsum('ijab,ijab', t2[ki, kj, ka], woovv)

            if ka != kb:
                eijba = eijab.transpose(0, 1, 3, 2)
                t2[ki, kj, kb] = eris_ijba.conj() / eijba
                woovv = 2 * eris_ijba - eris_ijab.transpose(0, 1, 3, 2)
                emp2 += np.einsum('ijab,ijab', t2[ki, kj, kb], woovv)

            touched[ki, kj, ka] = touched[ki, kj, kb] = True

        self.emp2 = emp2.real / nkpts
        logger.info(self, 'Init t2, MP2 energy (with fock eigenvalue shift) = %.15g', self.emp2)
        logger.timer(self, 'init mp2', *time0)
        return self.emp2, t1, t2

    energy = energy
    update_amps = update_amps

    def kernel(self, t1=None, t2=None, eris=None, mbpt2=False):
        return self.ccsd(t1, t2, eris, mbpt2=mbpt2)

    def ccsd(self, t1=None, t2=None, eris=None, mbpt2=False):
        '''Ground-state CCSD.

        Kwargs:
            mbpt2 : bool
                Use one-shot MBPT2 approximation to CCSD.
        '''
        self.dump_flags()
        if eris is None:
            # eris = self.ao2mo()
            eris = self.ao2mo(self.mo_coeff)
        self.eris = eris
        if mbpt2:
            cctyp = 'MBPT2'
            self.e_corr, self.t1, self.t2 = self.init_amps(eris)
            return self.e_corr, self.t1, self.t2

        cctyp = 'CCSD'
        self.converged, self.e_corr, self.t1, self.t2 = \
            kernel(self, eris, t1, t2, max_cycle=self.max_cycle,
                   tol=self.conv_tol, tolnormt=self.conv_tol_normt,
                   verbose=self.verbose)
        self._finalize()
        return self.e_corr, self.t1, self.t2

    def ao2mo(self, mo_coeff=None):
        return _ERIS(self, mo_coeff)

    ipccsd = kernel_ip
    eaccsd = kernel_ea

    @property
    def imds(self):
        if self.__imds__ is None:
            self.__imds__ = _IMDS(self)
        return self.__imds__


KRCCSD = RCCSD

class _ERIS:  # (pyscf.cc.ccsd._ChemistsERIs):
    def __init__(self, cc, mo_coeff=None, method='incore'):
        from pyscf.pbc import df
        from pyscf.pbc import tools
        from pyscf.pbc.cc.ccsd import _adjust_occ
        log = logger.Logger(cc.stdout, cc.verbose)
        cput0 = (time.clock(), time.time())
        moidx = get_frozen_mask(cc)
        cell = cc._scf.cell
        kpts = cc.kpts
        nkpts = cc.nkpts
        nocc = cc.nocc
        nmo = cc.nmo
        nvir = nmo - nocc

        # if any(nocc != np.count_nonzero(cc._scf.mo_occ[k]>0)
        #       for k in range(nkpts)):
        #    raise NotImplementedError('Different occupancies found for different k-points')

        if mo_coeff is None:
            mo_coeff = cc.mo_coeff
        dtype = mo_coeff[0].dtype

        mo_coeff = self.mo_coeff = padded_mo_coeff(cc, mo_coeff)

        # Re-make our fock MO matrix elements from density and fock AO
        dm = cc._scf.make_rdm1(cc.mo_coeff, cc.mo_occ)
        with lib.temporary_env(cc._scf, exxdiv=None):
            # _scf.exxdiv affects eris.fock. HF exchange correction should be
            # excluded from the Fock matrix.
            fockao = cc._scf.get_hcore() + cc._scf.get_veff(cell, dm)
        self.fock = np.asarray([reduce(np.dot, (mo.T.conj(), fockao[k], mo))
                                for k, mo in enumerate(mo_coeff)])

        self.mo_energy = [self.fock[k].diagonal().real for k in range(nkpts)]
        # Add HFX correction in the self.mo_energy to improve convergence in
        # CCSD iteration. It is useful for the 2D systems since their occupied and
        # the virtual orbital energies may overlap which may lead to numerical
        # issue in the CCSD iterations.
        # FIXME: Whether to add this correction for other exxdiv treatments?
        # Without the correction, MP2 energy may be largely off the correct value.
        madelung = tools.madelung(cell, kpts)
        self.mo_energy = [_adjust_occ(mo_e, nocc, -madelung)
                          for k, mo_e in enumerate(self.mo_energy)]

        # Get location of padded elements in occupied and virtual space.
        nocc_per_kpt = get_nocc(cc, per_kpoint=True)
        nonzero_padding = padding_k_idx(cc, kind="joint")

        # Check direct and indirect gaps for possible issues with CCSD convergence.
        mo_e = [self.mo_energy[kp][nonzero_padding[kp]] for kp in range(nkpts)]
        mo_e = np.sort([y for x in mo_e for y in x])  # Sort de-nested array
        gap = mo_e[np.sum(nocc_per_kpt)] - mo_e[np.sum(nocc_per_kpt)-1]
        if gap < 1e-5:
            logger.warn(cc, 'HOMO-LUMO gap %s too small for KCCSD. '
                            'May cause issues in convergence.', gap)

        mem_incore, mem_outcore, mem_basic = _mem_usage(nkpts, nocc, nvir)
        mem_now = lib.current_memory()[0]
        fao2mo = cc._scf.with_df.ao2mo

        kconserv = cc.khelper.kconserv
        khelper = cc.khelper
        orbo = np.asarray(mo_coeff[:,:,:nocc], order='C')
        orbv = np.asarray(mo_coeff[:,:,nocc:], order='C')

        if (method == 'incore' and (mem_incore + mem_now < cc.max_memory)
                or cell.incore_anyway):
            log.info('using incore ERI storage')
            self.oooo = np.empty((nkpts,nkpts,nkpts,nocc,nocc,nocc,nocc), dtype=dtype)
            self.ooov = np.empty((nkpts,nkpts,nkpts,nocc,nocc,nocc,nvir), dtype=dtype)
            self.oovv = np.empty((nkpts,nkpts,nkpts,nocc,nocc,nvir,nvir), dtype=dtype)
            self.ovov = np.empty((nkpts,nkpts,nkpts,nocc,nvir,nocc,nvir), dtype=dtype)
            self.voov = np.empty((nkpts,nkpts,nkpts,nvir,nocc,nocc,nvir), dtype=dtype)
            self.vovv = np.empty((nkpts,nkpts,nkpts,nvir,nocc,nvir,nvir), dtype=dtype)
            #self.vvvv = np.empty((nkpts,nkpts,nkpts,nvir,nvir,nvir,nvir), dtype=dtype)
            self.vvvv = cc._scf.with_df.ao2mo_7d(orbv, factor=1./nkpts).transpose(0,2,1,3,5,4,6)

            for (ikp,ikq,ikr) in khelper.symm_map.keys():
                iks = kconserv[ikp,ikq,ikr]
                eri_kpt = fao2mo((mo_coeff[ikp],mo_coeff[ikq],mo_coeff[ikr],mo_coeff[iks]),
                                 (kpts[ikp],kpts[ikq],kpts[ikr],kpts[iks]), compact=False)
                if dtype == np.float: eri_kpt = eri_kpt.real
                eri_kpt = eri_kpt.reshape(nmo, nmo, nmo, nmo)
                for (kp, kq, kr) in khelper.symm_map[(ikp, ikq, ikr)]:
                    eri_kpt_symm = khelper.transform_symm(eri_kpt, kp, kq, kr).transpose(0, 2, 1, 3)
                    self.oooo[kp, kr, kq] = eri_kpt_symm[:nocc, :nocc, :nocc, :nocc] / nkpts
                    self.ooov[kp, kr, kq] = eri_kpt_symm[:nocc, :nocc, :nocc, nocc:] / nkpts
                    self.oovv[kp, kr, kq] = eri_kpt_symm[:nocc, :nocc, nocc:, nocc:] / nkpts
                    self.ovov[kp, kr, kq] = eri_kpt_symm[:nocc, nocc:, :nocc, nocc:] / nkpts
                    self.voov[kp, kr, kq] = eri_kpt_symm[nocc:, :nocc, :nocc, nocc:] / nkpts
                    self.vovv[kp, kr, kq] = eri_kpt_symm[nocc:, :nocc, nocc:, nocc:] / nkpts
                    #self.vvvv[kp, kr, kq] = eri_kpt_symm[nocc:, nocc:, nocc:, nocc:] / nkpts

            self.dtype = dtype
        else:
            log.info('using HDF5 ERI storage')
            self.feri1 = lib.H5TmpFile()

            self.oooo = self.feri1.create_dataset('oooo', (nkpts, nkpts, nkpts, nocc, nocc, nocc, nocc), dtype.char)
            self.ooov = self.feri1.create_dataset('ooov', (nkpts, nkpts, nkpts, nocc, nocc, nocc, nvir), dtype.char)
            self.oovv = self.feri1.create_dataset('oovv', (nkpts, nkpts, nkpts, nocc, nocc, nvir, nvir), dtype.char)
            self.ovov = self.feri1.create_dataset('ovov', (nkpts, nkpts, nkpts, nocc, nvir, nocc, nvir), dtype.char)
            self.voov = self.feri1.create_dataset('voov', (nkpts, nkpts, nkpts, nvir, nocc, nocc, nvir), dtype.char)
            self.vovv = self.feri1.create_dataset('vovv', (nkpts, nkpts, nkpts, nvir, nocc, nvir, nvir), dtype.char)

            if True:#not (cc.direct and type(cc._scf.with_df) is df.GDF):
                self.vvvv = self.feri1.create_dataset('vvvv', (nkpts,nkpts,nkpts,nvir,nvir,nvir,nvir), dtype.char)

            # <ij|pq>  = (ip|jq)
            cput1 = time.clock(), time.time()
            for kp in range(nkpts):
                for kq in range(nkpts):
                    for kr in range(nkpts):
                        ks = kconserv[kp, kq, kr]
                        orbo_p = mo_coeff[kp][:, :nocc]
                        orbo_r = mo_coeff[kr][:, :nocc]
                        buf_kpt = fao2mo((orbo_p, mo_coeff[kq], orbo_r, mo_coeff[ks]),
                                         (kpts[kp], kpts[kq], kpts[kr], kpts[ks]), compact=False)
                        if mo_coeff[0].dtype == np.float: buf_kpt = buf_kpt.real
                        buf_kpt = buf_kpt.reshape(nocc, nmo, nocc, nmo).transpose(0, 2, 1, 3)
                        self.dtype = buf_kpt.dtype
                        self.oooo[kp, kr, kq, :, :, :, :] = buf_kpt[:, :, :nocc, :nocc] / nkpts
                        self.ooov[kp, kr, kq, :, :, :, :] = buf_kpt[:, :, :nocc, nocc:] / nkpts
                        self.oovv[kp, kr, kq, :, :, :, :] = buf_kpt[:, :, nocc:, nocc:] / nkpts
            cput1 = log.timer_debug1('transforming oopq', *cput1)

            # <ia|pq> = (ip|aq)
            cput1 = time.clock(), time.time()
            for kp in range(nkpts):
                for kq in range(nkpts):
                    for kr in range(nkpts):
                        ks = kconserv[kp, kq, kr]
                        orbo_p = mo_coeff[kp][:, :nocc]
                        orbv_r = mo_coeff[kr][:, nocc:]
                        buf_kpt = fao2mo((orbo_p, mo_coeff[kq], orbv_r, mo_coeff[ks]),
                                         (kpts[kp], kpts[kq], kpts[kr], kpts[ks]), compact=False)
                        if mo_coeff[0].dtype == np.float: buf_kpt = buf_kpt.real
                        buf_kpt = buf_kpt.reshape(nocc, nmo, nvir, nmo).transpose(0, 2, 1, 3)
                        self.ovov[kp, kr, kq, :, :, :, :] = buf_kpt[:, :, :nocc, nocc:] / nkpts
                        # TODO: compute vovv on the fly
                        self.vovv[kr, kp, ks, :, :, :, :] = buf_kpt[:, :, nocc:, nocc:].transpose(1, 0, 3, 2) / nkpts
                        self.voov[kr, kp, ks, :, :, :, :] = buf_kpt[:, :, nocc:, :nocc].transpose(1, 0, 3, 2) / nkpts
            cput1 = log.timer_debug1('transforming ovpq', *cput1)

            ## Without k-point symmetry
            # cput1 = time.clock(), time.time()
            # for kp in range(nkpts):
            #    for kq in range(nkpts):
            #        for kr in range(nkpts):
            #            ks = kconserv[kp,kq,kr]
            #            orbv_p = mo_coeff[kp][:,nocc:]
            #            orbv_q = mo_coeff[kq][:,nocc:]
            #            orbv_r = mo_coeff[kr][:,nocc:]
            #            orbv_s = mo_coeff[ks][:,nocc:]
            #            for a in range(nvir):
            #                orbva_p = orbv_p[:,a].reshape(-1,1)
            #                buf_kpt = fao2mo((orbva_p,orbv_q,orbv_r,orbv_s),
            #                                 (kpts[kp],kpts[kq],kpts[kr],kpts[ks]), compact=False)
            #                if mo_coeff[0].dtype == np.float: buf_kpt = buf_kpt.real
            #                buf_kpt = buf_kpt.reshape((1,nvir,nvir,nvir)).transpose(0,2,1,3)
            #                self.vvvv[kp,kr,kq,a,:,:,:] = buf_kpt[:] / nkpts
            # cput1 = log.timer_debug1('transforming vvvv', *cput1)

            cput1 = time.clock(), time.time()
            mem_now = lib.current_memory()[0]
            if False:#cc.direct and type(cc._scf.with_df) is df.GDF:
                _init_df_eris(cc, self)

            elif nvir ** 4 * 16 / 1e6 + mem_now < cc.max_memory:
                for (ikp, ikq, ikr) in khelper.symm_map.keys():
                    iks = kconserv[ikp, ikq, ikr]
                    orbv_p = mo_coeff[ikp][:, nocc:]
                    orbv_q = mo_coeff[ikq][:, nocc:]
                    orbv_r = mo_coeff[ikr][:, nocc:]
                    orbv_s = mo_coeff[iks][:, nocc:]
                    # unit cell is small enough to handle vvvv in-core
                    buf_kpt = fao2mo((orbv_p,orbv_q,orbv_r,orbv_s),
                                     kpts[[ikp,ikq,ikr,iks]], compact=False)
                    if dtype == np.float: buf_kpt = buf_kpt.real
                    buf_kpt = buf_kpt.reshape((nvir, nvir, nvir, nvir))
                    for (kp, kq, kr) in khelper.symm_map[(ikp, ikq, ikr)]:
                        buf_kpt_symm = khelper.transform_symm(buf_kpt, kp, kq, kr).transpose(0, 2, 1, 3)
                        self.vvvv[kp, kr, kq] = buf_kpt_symm / nkpts
            else:
                raise MemoryError('Minimal memory requirements %s MB'
                                  % (mem_now + nvir ** 4 / 1e6 * 16 * 2))
                for (ikp, ikq, ikr) in khelper.symm_map.keys():
                    for a in range(nvir):
                        orbva_p = orbv_p[:, a].reshape(-1, 1)
                        buf_kpt = fao2mo((orbva_p, orbv_q, orbv_r, orbv_s),
                                         (kpts[ikp], kpts[ikq], kpts[ikr], kpts[iks]), compact=False)
                        if mo_coeff[0].dtype == np.float: buf_kpt = buf_kpt.real
                        buf_kpt = buf_kpt.reshape((1, nvir, nvir, nvir)).transpose(0, 2, 1, 3)

                        self.vvvv[ikp, ikr, ikq, a, :, :, :] = buf_kpt[0, :, :, :] / nkpts
                        # Store symmetric permutations
                        self.vvvv[ikr, ikp, iks, :, a, :, :] = buf_kpt.transpose(1, 0, 3, 2)[:, 0, :, :] / nkpts
                        self.vvvv[ikq, iks, ikp, :, :, a, :] = buf_kpt.transpose(2, 3, 0, 1).conj()[:, :, 0, :] / nkpts
                        self.vvvv[iks, ikq, ikr, :, :, :, a] = buf_kpt.transpose(3, 2, 1, 0).conj()[:, :, :, 0] / nkpts
            cput1 = log.timer_debug1('transforming vvvv', *cput1)

        log.timer('CCSD integral transformation', *cput0)


def _init_df_eris(cc, eris):
    from pyscf.pbc.df import df
    from pyscf.ao2mo import _ao2mo
    if cc._scf.with_df._cderi is None:
        cc._scf.with_df.build()

    cell = cc._scf.cell
    if cell.dimension == 2:
        raise NotImplementedError

    nocc = cc.nocc
    nmo = cc.nmo
    nvir = nmo - nocc
    nao = cell.nao_nr()

    kpts = cc.kpts
    nkpts = len(kpts)
    naux = cc._scf.with_df.get_naoaux()
    if gamma_point(kpts):
        dtype = np.double
    else:
        dtype = np.complex128
    dtype = np.result_type(dtype, *eris.mo_coeff)
    eris.Lpv = np.empty((nkpts, nkpts, naux, nmo, nvir), dtype=dtype)

    with h5py.File(cc._scf.with_df._cderi, 'r') as f:
        kptij_lst = f['j3c-kptij'].value
        tao = []
        ao_loc = None
        for ki, kpti in enumerate(kpts):
            for kj, kptj in enumerate(kpts):
                kpti_kptj = np.array((kpti, kptj))
                Lpq = np.asarray(df._getitem(f, 'j3c', kpti_kptj, kptij_lst))

                mo = np.hstack((eris.mo_coeff[ki], eris.mo_coeff[kj][:, nocc:]))
                mo = np.asarray(mo, dtype=dtype, order='F')
                if dtype == np.double:
                    _ao2mo.nr_e2(Lpq, mo, (0, nmo, nmo, nmo + nvir), aosym='s2',
                                 out=eris.Lpv[ki, kj])
                else:
                    if Lpq.size != naux * nao ** 2:  # aosym = 's2'
                        Lpq = lib.unpack_tril(Lpq).astype(np.complex128)
                    _ao2mo.r_e2(Lpq, mo, (0, nmo, nmo, nmo + nvir), tao, ao_loc,
                                out=eris.Lpv[ki, kj])
    return eris


imd = imdk


class _IMDS:
    # Identical to molecular rccsd_slow
    def __init__(self, cc):
        self.verbose = cc.verbose
        self.stdout = cc.stdout
        self.t1 = cc.t1
        self.t2 = cc.t2
        self.eris = cc.eris
        self.kconserv = cc.khelper.kconserv
        self.made_ip_imds = False
        self.made_ea_imds = False
        self._made_shared_2e = False
        # TODO: check whether to hold all stuff in memory
        self._fimd = lib.H5TmpFile() if hasattr(self.eris, "feri1") else None

    def _make_shared_1e(self):
        cput0 = (time.clock(), time.time())
        log = logger.Logger(self.stdout, self.verbose)

        t1, t2, eris = self.t1, self.t2, self.eris
        kconserv = self.kconserv
        self.Loo = imd.Loo(t1, t2, eris, kconserv)
        self.Lvv = imd.Lvv(t1, t2, eris, kconserv)
        self.Fov = imd.cc_Fov(t1, t2, eris, kconserv)

        log.timer('EOM-CCSD shared one-electron intermediates', *cput0)

    def _make_shared_2e(self):
        cput0 = (time.clock(), time.time())
        log = logger.Logger(self.stdout, self.verbose)

        t1, t2, eris = self.t1, self.t2, self.eris
        kconserv = self.kconserv

        if self._fimd is not None:
            nkpts, nocc, nvir = t1.shape
            ovov_dest = self._fimd.create_dataset('ovov', (nkpts, nkpts, nkpts, nocc, nvir, nocc, nvir), t1.dtype.char)
            ovvo_dest = self._fimd.create_dataset('ovvo', (nkpts, nkpts, nkpts, nocc, nvir, nvir, nocc), t1.dtype.char)
        else:
            ovov_dest = ovvo_dest = None

        # 2 virtuals
        self.Wovov = imd.Wovov(t1, t2, eris, kconserv, ovov_dest)
        self.Wovvo = imd.Wovvo(t1, t2, eris, kconserv, ovvo_dest)
        self.Woovv = eris.oovv

        log.timer('EOM-CCSD shared two-electron intermediates', *cput0)

    def make_ip(self, ip_partition=None):
        self._make_shared_1e()
        if self._made_shared_2e is False and ip_partition != 'mp':
            self._make_shared_2e()
            self._made_shared_2e = True

        cput0 = (time.clock(), time.time())
        log = logger.Logger(self.stdout, self.verbose)

        t1, t2, eris = self.t1, self.t2, self.eris
        kconserv = self.kconserv

        if self._fimd is not None:
            nkpts, nocc, nvir = t1.shape
            oooo_dest = self._fimd.create_dataset('oooo', (nkpts, nkpts, nkpts, nocc, nocc, nocc, nocc), t1.dtype.char)
            ooov_dest = self._fimd.create_dataset('ooov', (nkpts, nkpts, nkpts, nocc, nocc, nocc, nvir), t1.dtype.char)
            ovoo_dest = self._fimd.create_dataset('ovoo', (nkpts, nkpts, nkpts, nocc, nvir, nocc, nocc), t1.dtype.char)
        else:
            oooo_dest = ooov_dest = ovoo_dest = None

        # 0 or 1 virtuals
        if ip_partition != 'mp':
            self.Woooo = imd.Woooo(t1, t2, eris, kconserv, oooo_dest)
        self.Wooov = imd.Wooov(t1, t2, eris, kconserv, ooov_dest)
        self.Wovoo = imd.Wovoo(t1, t2, eris, kconserv, ovoo_dest)
        self.made_ip_imds = True
        log.timer('EOM-CCSD IP intermediates', *cput0)

    def make_ea(self, ea_partition=None):
        self._make_shared_1e()
        if self._made_shared_2e is False and ea_partition != 'mp':
            self._make_shared_2e()
            self._made_shared_2e = True

        cput0 = (time.clock(), time.time())
        log = logger.Logger(self.stdout, self.verbose)

        t1, t2, eris = self.t1, self.t2, self.eris
        kconserv = self.kconserv

        if self._fimd is not None:
            nkpts, nocc, nvir = t1.shape
            vovv_dest = self._fimd.create_dataset('vovv', (nkpts, nkpts, nkpts, nvir, nocc, nvir, nvir), t1.dtype.char)
            vvvo_dest = self._fimd.create_dataset('vvvo', (nkpts, nkpts, nkpts, nvir, nvir, nvir, nocc), t1.dtype.char)
            vvvv_dest = self._fimd.create_dataset('vvvv', (nkpts, nkpts, nkpts, nvir, nvir, nvir, nvir), t1.dtype.char)
        else:
            vovv_dest = vvvo_dest = vvvv_dest = None

        # 3 or 4 virtuals
        self.Wvovv = imd.Wvovv(t1, t2, eris, kconserv, vovv_dest)
        if ea_partition == 'mp' and np.all(t1 == 0):
            self.Wvvvo = imd.Wvvvo(t1, t2, eris, kconserv, vvvo_dest)
        else:
            self.Wvvvv = imd.Wvvvv(t1, t2, eris, kconserv, vvvv_dest)
            self.Wvvvo = imd.Wvvvo(t1, t2, eris, kconserv, self.Wvvvv, vvvo_dest)
        self.made_ea_imds = True
        log.timer('EOM-CCSD EA intermediates', *cput0)

    def make_ee(self):
        raise NotImplementedError


def _mem_usage(nkpts, nocc, nvir):
    incore = nkpts ** 3 * (nocc + nvir) ** 4
    # Roughly, factor of two for intermediates and factor of two
    # for safety (temp arrays, copying, etc)
    incore *= 4
    # TODO: Improve incore estimate and add outcore estimate
    outcore = basic = incore
    return incore * 16 / 1e6, outcore * 16 / 1e6, basic * 16 / 1e6


if __name__ == '__main__':
    from pyscf.pbc import gto, scf, cc

    cell = gto.Cell()
    cell.atom = '''
    C 0.000000000000   0.000000000000   0.000000000000
    C 1.685068664391   1.685068664391   1.685068664391
    '''
    cell.basis = {'C': [[0, (0.8, 1.0)],
                        [1, (1.0, 1.0)]]}
    cell.pseudo = 'gth-pade'
    cell.a = '''
    0.000000000, 3.370137329, 3.370137329
    3.370137329, 0.000000000, 3.370137329
    3.370137329, 3.370137329, 0.000000000'''
    cell.unit = 'B'
    cell.verbose = 5
    cell.build()

    # Running HF and CCSD with 1x1x2 Monkhorst-Pack k-point mesh
    kmf = scf.KRHF(cell, kpts=cell.make_kpts([1, 1, 2]), exxdiv=None)
    ehf = kmf.kernel()

    mycc = cc.KRCCSD(kmf)
    mycc.conv_tol = 1e-10
    mycc.conv_tol_normt = 1e-10
    ecc, t1, t2 = mycc.kernel()
    print(ecc - -0.155298393321855)

    e_ip, _ = mycc.ipccsd(nroots=3, kptlist=(0,))
    e_ea, _ = mycc.eaccsd(nroots=3, kptlist=(0,))
    print(e_ip, e_ea)

    ####
    cell = gto.Cell()
    cell.atom = '''
    He 0.000000000000   0.000000000000   0.000000000000
    He 1.685068664391   1.685068664391   1.685068664391
    '''
    cell.basis = [[0, (1., 1.)], [0, (.5, 1.)]]
    cell.a = '''
    0.000000000, 3.370137329, 3.370137329
    3.370137329, 0.000000000, 3.370137329
    3.370137329, 3.370137329, 0.000000000'''
    cell.unit = 'B'
    cell.build()

    np.random.seed(2)
    # Running HF and CCSD with 1x1x2 Monkhorst-Pack k-point mesh
    kmf = scf.KRHF(cell, kpts=cell.make_kpts([1, 1, 3]), exxdiv=None)
    nmo = cell.nao_nr()
    kmf.mo_occ = np.zeros((3, nmo))
    kmf.mo_occ[:, :2] = 2
    kmf.mo_energy = np.arange(nmo) + np.random.random((3, nmo)) * .3
    kmf.mo_energy[kmf.mo_occ == 0] += 2
    kmf.mo_coeff = (np.random.random((3, nmo, nmo)) +
                    np.random.random((3, nmo, nmo)) * 1j - .5 - .5j)


    def rand_t1_t2(mycc):
        nkpts = mycc.nkpts
        nocc = mycc.nocc
        nmo = mycc.nmo
        nvir = nmo - nocc
        np.random.seed(1)
        t1 = (np.random.random((nkpts, nocc, nvir)) +
              np.random.random((nkpts, nocc, nvir)) * 1j - .5 - .5j)
        t2 = (np.random.random((nkpts, nkpts, nkpts, nocc, nocc, nvir, nvir)) +
              np.random.random((nkpts, nkpts, nkpts, nocc, nocc, nvir, nvir)) * 1j - .5 - .5j)
        kconserv = kpts_helper.get_kconserv(kmf.cell, kmf.kpts)
        Ps = kconserve_pmatrix(nkpts, kconserv)
        t2 = t2 + np.einsum('xyzijab,xyzw->yxwjiba', t2, Ps)
        return t1, t2


    mycc = KRCCSD(kmf)
    eris = mycc.ao2mo()
    t1, t2 = rand_t1_t2(mycc)
    Ht1, Ht2 = mycc.update_amps(t1, t2, eris)
    print(lib.finger(Ht1) - (-4.6808039711608824 + 9.4962987225515789j))  # FIXME
    print(lib.finger(Ht2) - (18.613685230812546 + 114.66975731912211j))  # FIXME

    kmf = kmf.density_fit(auxbasis=[[0, (1., 1.)], [0, (.5, 1.)]])
    mycc = KRCCSD(kmf)
    eris = _ERIS(mycc, mycc.mo_coeff, method='outcore')
    t1, t2 = rand_t1_t2(mycc)
    Ht1, Ht2 = mycc.update_amps(t1, t2, eris)
    print(lib.finger(Ht1) - (-3.6611794882508244 + 9.2241044317516554j))  # FIXME
    print(lib.finger(Ht2) - (-196.88536721771101 - 432.29569128644886j))  # FIXME
