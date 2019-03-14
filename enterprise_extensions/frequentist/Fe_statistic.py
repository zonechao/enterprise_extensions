from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
import numpy as np
import scipy.linalg as sl
import json

#from enterprise_extensions import models
import enterprise_cw_funcs_from_git as models

import enterprise
from enterprise.pulsar import Pulsar
import enterprise.signals.parameter as parameter
from enterprise.signals import utils
from enterprise.signals import signal_base
from enterprise.signals import selections
from enterprise.signals.selections import Selection
from enterprise.signals import white_signals
from enterprise.signals import gp_signals
from enterprise.signals import deterministic_signals
import enterprise.constants as const

class FeStat(object):
    """
    Class for the Fe-statistic.
    :param psrs: List of `enterprise` Pulsar instances.
    """
    
    def __init__(self, psrs, params=None):
        
        # initialize standard model with fixed white noise and powerlaw red noise
        print('Initializing the model...')

        #TODO: make noise parameters setable from outside and maybe remove the
        #signal model part alltogether
        efac = parameter.Constant(1.04) 
        #efac = parameter.Constant(1.0) 
        equad = parameter.Constant(-7) 
        ef = white_signals.MeasurementNoise(efac=efac)
        eq = white_signals.EquadNoise(log10_equad=equad)
        log10_fgw = parameter.Uniform(np.log10(3.5e-9), -7)('log10_fgw')


        log10_mc = parameter.Constant(np.log10(5e9))('log10_mc')
        cos_gwtheta = parameter.Uniform(-1, 1)('cos_gwtheta')
        gwphi = parameter.Uniform(0, 2*np.pi)('gwphi')
        phase0 = parameter.Uniform(0, 2*np.pi)('phase0')
        psi = parameter.Uniform(0, np.pi)('psi')
        cos_inc = parameter.Uniform(-1, 1)('cos_inc')
        log10_h = parameter.LinearExp(-18, -11)('log10_h')
        cw_wf = models.cw_delay(cos_gwtheta=cos_gwtheta, gwphi=gwphi, log10_mc=log10_mc,
                             log10_h=log10_h, log10_fgw=log10_fgw, phase0=phase0,
                             psi=psi, cos_inc=cos_inc, tref=53000*86400)
        cw = models.CWSignal(cw_wf, psrTerm=False)

        tm = gp_signals.TimingModel(use_svd=True)

        s = eq + ef + tm + cw
        #s = ef + eq + cw

        #number of pulsars to use
        n_psr = 19
        #n_psr = 3

        model = []
        for p in psrs[:n_psr]:
            model.append(s(p))
        self.pta = signal_base.PTA(model)  

        self.psrs = psrs
        self.params = params
                                   
        self.Nmats = None


    def get_Nmats(self):
        '''Makes the Nmatrix used in the fstatistic'''
        TNTs = self.pta.get_TNT(self.params)
        phiinvs = self.pta.get_phiinv(self.params, logdet=False, method='partition')
        #Get noise parameters for pta toaerr**2
        Nvecs = self.pta.get_ndiag(self.params)
        #Get the basis matrix
        Ts = self.pta.get_basis(self.params)
        
        Nmats = [ make_Nmat(phiinv, TNT, Nvec, T) for phiinv, TNT, Nvec, T in zip(phiinvs, TNTs, Nvecs, Ts)]
        
        return Nmats

    def compute_Fe(self, f0, gw_skyloc, brave=False, maximized_parameters=False):
        """
        Computes the Fe-statistic (see Ellis, Siemens, Creighton 2012).
        :param f0: GW frequency
        :param gw_skyloc: [theta, phi] or 2x{number of sky locations} array,
                          where theta=pi/2-DEC, phi=RA
        :param brave: Skip sanity checks in linalg for speedup if True.
        :param maximized_parameters: Calculate maximized parameters if True.
        :returns:
        fstat: value of the Fe-statistic
        """

        tref=53000*86400
        
        phiinvs = self.pta.get_phiinv(self.params, logdet=False)
        TNTs = self.pta.get_TNT(self.params)
        Ts = self.pta.get_basis()
        
        if self.Nmats == None:
            
            self.Nmats = self.get_Nmats()
        
        n_psr = len(self.psrs)
        N = np.zeros((n_psr,4))
        M = np.zeros((n_psr,4,4))
        
        for idx, (psr, Nmat, TNT, phiinv, T) in enumerate(zip(self.psrs, self.Nmats,
                                             TNTs, phiinvs, Ts)):
            
            Sigma = TNT + (np.diag(phiinv) if phiinv.ndim == 1 else phiinv)
            
            ntoa = len(psr.toas)

            A = np.zeros((4, ntoa))
            A[0, :] = 1 / f0 ** (1 / 3) * np.sin(2 * np.pi * f0 * (psr.toas-tref))
            A[1, :] = 1 / f0 ** (1 / 3) * np.cos(2 * np.pi * f0 * (psr.toas-tref))
            A[2, :] = 1 / f0 ** (1 / 3) * np.sin(2 * np.pi * f0 * (psr.toas-tref))
            A[3, :] = 1 / f0 ** (1 / 3) * np.cos(2 * np.pi * f0 * (psr.toas-tref))

            ip1 = innerProduct_rr(A[0, :], psr.residuals, Nmat, T, Sigma, brave=brave)
            ip2 = innerProduct_rr(A[1, :], psr.residuals, Nmat, T, Sigma, brave=brave)
            ip3 = innerProduct_rr(A[2, :], psr.residuals, Nmat, T, Sigma, brave=brave)
            ip4 = innerProduct_rr(A[3, :], psr.residuals, Nmat, T, Sigma, brave=brave)
            
            N[idx, :] = np.array([ip1, ip2, ip3, ip4])
                                  
            # define M matrix M_ij=(A_i|A_j)
            for jj in range(4):
                for kk in range(4):
                    M[idx, jj, kk] = innerProduct_rr(A[jj, :], A[kk, :], Nmat, T, Sigma, brave=brave)

        fstat = np.zeros(gw_skyloc.shape[1])
        if maximized_parameters:
            inc_max = np.zeros(gw_skyloc.shape[1])
            psi_max = np.zeros(gw_skyloc.shape[1])
            phase0_max = np.zeros(gw_skyloc.shape[1])
            h_max = np.zeros(gw_skyloc.shape[1])

        for j, gw_pos in enumerate(gw_skyloc.T):
            NN = np.copy(N)
            MM = np.copy(M)
            for idx, psr in enumerate(self.psrs):
                F_p, F_c, _ = utils.create_gw_antenna_pattern(psr.pos, gw_pos[0], gw_pos[1])
                NN[idx, :] *= np.array([F_p, F_p, F_c, F_c])
                MM[idx,:,:] *= np.array([[F_p**2, F_p**2, F_p*F_c, F_p*F_c],
                                      [F_p**2, F_p**2, F_p*F_c, F_p*F_c],
                                      [F_p*F_c, F_p*F_c, F_c**2, F_c**2],
                                      [F_p*F_c, F_p*F_c, F_c**2, F_c**2]])

            N_sum = np.sum(NN,axis=0)
            M_sum = np.sum(MM,axis=0)

            # take inverse of M
            Minv = np.linalg.pinv(M_sum)

            fstat[j] = 0.5 * np.dot(N_sum, np.dot(Minv, N_sum))
            
            if maximized_parameters:
                a_hat = np.dot(Minv, N_sum)
                
                A_p = (np.sqrt((a_hat[0]+a_hat[3])**2 + (a_hat[1]-a_hat[2])**2) +
                       np.sqrt((a_hat[0]-a_hat[3])**2 + (a_hat[1]+a_hat[2])**2))
                A_c = (np.sqrt((a_hat[0]+a_hat[3])**2 + (a_hat[1]-a_hat[2])**2) -
                       np.sqrt((a_hat[0]-a_hat[3])**2 + (a_hat[1]+a_hat[2])**2))
                AA = A_p + np.sqrt(A_p**2 - A_c**2)
                #AA = A_p + np.sqrt(A_p**2 + A_c**2)

                #inc_max[j] = np.arccos(-A_c/AA)
                inc_max[j] = np.arccos(A_c/AA)

                two_psi_max = np.arctan2((A_p*a_hat[3] - A_c*a_hat[0]),
                                           (A_c*a_hat[2] + A_p*a_hat[1]))

                psi_max[j]=0.5*np.arctan2(np.sin(two_psi_max),
                                         -np.cos(two_psi_max))

                #convert from [-pi, pi] convention to [0,2*pi] convention
                if psi_max[j]<0:
                    psi_max[j]+=np.pi

                #correcting weird problem of degeneracy (psi-->pi-psi/2 and phi0-->2pi-phi0 keep everything the same)
                if psi_max[j]>np.pi/2:
                    psi_max[j]+= -np.pi/2
                

                half_phase0 = -0.5*np.arctan2(A_p*a_hat[3] - A_c*a_hat[0],
                                                A_c*a_hat[1] + A_p*a_hat[2])

                phase0_max[j] = np.arctan2(-np.sin(2*half_phase0),
                                           np.cos(2*half_phase0))
                
                #convert from [-pi, pi] convention to [0,2*pi] convention
                if phase0_max[j]<0:
                    phase0_max[j]+=2*np.pi

                
                zeta = np.abs(AA)/4 #related to amplitude, zeta=M_chirp^(5/3)/D
                h_max[j] = zeta * 2 * (np.pi*f0)**(2/3)*np.pi**(1/3)

        if maximized_parameters:
            return fstat, inc_max, psi_max, phase0_max, h_max
        else:
            return fstat


def innerProduct_rr(x, y, Nmat, Tmat, Sigma, TNx=None, TNy=None, brave=False):
    """
        Compute inner product using rank-reduced
        approximations for red noise/jitter
        Compute: x^T N^{-1} y - x^T N^{-1} T \Sigma^{-1} T^T N^{-1} y
        
        :param x: vector timeseries 1
        :param y: vector timeseries 2
        :param Nmat: white noise matrix
        :param Tmat: Modified design matrix including red noise/jitter
        :param Sigma: Sigma matrix (\varphi^{-1} + T^T N^{-1} T)
        :param TNx: T^T N^{-1} x precomputed
        :param TNy: T^T N^{-1} y precomputed
        :return: inner product (x|y)
        """
    
    # white noise term
    Ni = Nmat
    xNy = np.dot(np.dot(x, Ni), y)
    Nx, Ny = np.dot(Ni, x), np.dot(Ni, y)
    
    if TNx == None and TNy == None:
        TNx = np.dot(Tmat.T, Nx)
        TNy = np.dot(Tmat.T, Ny)
    
    if brave:
        cf = sl.cho_factor(Sigma, check_finite=False)
        SigmaTNy = sl.cho_solve(cf, TNy, check_finite=False)
    else:
        cf = sl.cho_factor(Sigma)
        SigmaTNy = sl.cho_solve(cf, TNy)

    ret = xNy - np.dot(TNx, SigmaTNy)

    return ret

def make_Nmat(phiinv, TNT, Nvec, T):
    
    Sigma = TNT + (np.diag(phiinv) if phiinv.ndim == 1 else phiinv)   
    cf = sl.cho_factor(Sigma)
    Nshape = np.shape(T)[0]

    TtN = Nvec.solve(other = np.eye(Nshape),left_array = T)
    
    #Put pulsar's autoerrors in a diagonal matrix
    Ndiag = Nvec.solve(other = np.eye(Nshape),left_array = np.eye(Nshape))
    
    expval2 = sl.cho_solve(cf,TtN)
    #TtNt = np.transpose(TtN)
    
    #An Ntoa by Ntoa noise matrix to be used in expand dense matrix calculations earlier
    return Ndiag - np.dot(TtN.T,expval2)

