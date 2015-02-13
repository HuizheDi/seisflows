
import subprocess
from os.path import join
from glob import glob

import numpy as np

import seisflows.seistools.specfem2d as solvertools
from seisflows.seistools.shared import getpar, setpar
from seisflows.seistools.io import splitvec

from seisflows.tools import unix
from seisflows.tools.array import loadnpy, savenpy
from seisflows.tools.code import exists
from seisflows.tools.config import findpath, loadclass, ParameterObj

PAR = ParameterObj('SeisflowsParameters')
PATH = ParameterObj('SeisflowsPaths')

import system
import preprocess


class specfem2d(loadclass('solver', 'base')):
    """ Python interface for SPECFEM2D

      See base class for method descriptions
    """

    parameters = []
    parameters += ['vs']

    density_scaling = None


    def check(self):
        """ Checks parameters, paths, and dependencies
        """
        super(specfem2d, self).check()

        # check time stepping parameters
        if 'NT' not in PAR:
            raise Exception

        if 'DT' not in PAR:
            raise Exception

        if 'F0' not in PAR:
            raise Exception

        # check solver executables directory
        if 'SPECFEM2D_BIN' not in PATH:
            pass #raise Exception

        # check solver input files directory
        if 'SPECFEM2D_DATA' not in PATH:
            pass #raise Exception


    def generate_data(self, **model_kwargs):
        """ Generates data
        """
        self.generate_mesh(**model_kwargs)

        unix.cd(self.getpath)
        setpar('SIMULATION_TYPE', '1')
        setpar('SAVE_FORWARD', '.true.')
        self.mpirun('bin/xmeshfem2D')
        self.mpirun('bin/xspecfem2D')

        unix.mv(self.data_wildcard, 'traces/obs')
        self.export_traces(PATH.OUTPUT, 'traces/obs')


    def generate_mesh(self, model_path=None, model_name=None, model_type='gll'):
        """ Performs meshing and database generation
        """
        assert(model_name)
        assert(model_type)
        assert (exists(model_path))

        self.initialize_solver_directories()
        unix.cp(model_path, 'DATA/model_velocity.dat_input')
        self.export_model(PATH.OUTPUT +'/'+ model_name)


    ### low-level solver interface

    def forward(self):
        """ Calls SPECFEM2D forward solver
        """
        setpar('SIMULATION_TYPE', '1')
        setpar('SAVE_FORWARD', '.true.')
        self.mpirun('bin/xmeshfem2D')
        self.mpirun('bin/xspecfem2D')


    def adjoint(self):
        """ Calls SPECFEM2D adjoint solver
        """
        setpar('SIMULATION_TYPE', '3')
        setpar('SAVE_FORWARD', '.false.')
        unix.rm('SEM')
        unix.ln('traces/adj', 'SEM')

        self.mpirun('bin/xmeshfem2D')
        self.mpirun('bin/xspecfem2D')


    ### model input/output

    def load(self, filename, mapping=None, suffix='', verbose=False):
        """Reads SPECFEM2D kernel or model

           Models and kernels are read from 5 or 6 column text files whose
           format is described in the SPECFEM2D user manual. Once read, a model
           or kernel is stored in a dictionary containing mesh coordinates and
           corresponding material parameter values.
        """
        # read text file
        M = np.loadtxt(filename)
        nrow = M.shape[0]
        ncol = M.shape[1]

        if ncol == 5:
            ioff = 0
        elif ncol == 6:
            ioff = 1
        else:
            raise Exception('Bad SPECFEM2D model or kernel.')

        # fill in dictionary
        model = {}
        for key in ['x', 'z', 'rho', 'vp', 'vs']:
            model[key] = [M[:,ioff]]
            ioff += 1
        return model


    def save(self, filename, model, type='model'):
        """ writes SPECFEM2D kernel or model
        """
        # allocate array
        if type == 'model':
            nrow = len(model[model.keys().pop()][0])
            ncol = 6
            ioff = 1
            M = np.zeros((nrow, ncol))
        elif type == 'kernel':
            nrow = len(model[model.keys().pop()][0])
            ncol = 5
            ioff = 0
            M = np.zeros((nrow, ncol))
        else:
            raise ValueError

        # fill in array
        for icol, key in enumerate(('x', 'z', 'rho', 'vp', 'vs')):
            if key in model.keys():
                M[:,icol+ioff] = model[key][0]
            else:
                M[:,icol+ioff] = loadbyproc(PATH.MODEL_INIT, key)

        # write array
        np.savetxt(filename, M, '%16.10e')



    ### postprocessing utilities

    def combine(self, path=''):
        """ Combines SPECFEM2D kernels
        """
        subprocess.call(
            [self.getpath +'/'+ 'bin/xsmooth_sem'] +
            [str(len(unix.ls(path)))] +
            [path])


    def smooth(self, path='', tag='gradient', span=0.):
        """ Smooths SPECFEM2D kernels by convolving them with a Gaussian
        """
        from seisflows.tools.array import meshsmooth

        parts = self.load(path +'/'+ tag)
        if not span:
            return parts

        # set up grid
        x = parts['x'][0]
        z = parts['z'][0]
        lx = x.max() - x.min()
        lz = z.max() - z.min()
        nn = x.size
        nx = np.around(np.sqrt(nn*lx/lz))
        nz = np.around(np.sqrt(nn*lx/lz))

        # perform smoothing
        for key in self.parameters:
            parts[key] = [meshsmooth(x, z, parts[key][0], span, nx, nz)]
        unix.mv(path +'/'+ tag, path +'/'+ '_nosmooth')
        self.save(path +'/'+ tag, parts)


    def clip(self, path='', tag='gradient', thresh=1.):
        """clips SPECFEM2D kernels"""
        parts = self.load(path +'/'+ tag)
        if thresh >= 1.:
            return parts

        for key in self.parameters:
            # scale to [-1,1]
            minval = parts[key][0].min()
            maxval = parts[key][0].max()
            np.clip(parts[key][0], thresh*minval, thresh*maxval, out=parts[key][0])
        unix.mv(path +'/'+ tag, path +'/'+ '_noclip')
        self.save(path +'/'+ tag, parts)


    ### file transfer utilities

    def import_model(self, path):
        src = join(path +'/'+ 'model')
        dst = join(self.getpath, 'DATA/model_velocity.dat_input')
        unix.cp(src, dst)

    def import_traces(self, path):
        src = glob(join(path, 'traces', self.getname, '*'))
        dst = join(self.getpath, 'traces/obs')
        unix.cp(src, dst)

    def export_model(self, path):
        if system.getnode() == 0:
            src = join(self.getpath, 'DATA/model_velocity.dat_input')
            dst = path
            unix.cp(src, dst)

    def export_kernels(self, path):
        unix.mkdir_gpfs(join(path, 'kernels'))
        src = join(self.getpath, 'OUTPUT_FILES/proc000000_rhop_alpha_beta_kernel.dat')
        dst = join(path, 'kernels', '%06d' % system.getnode())
        unix.cp(src, dst)

    def export_residuals(self, path):
        unix.mkdir_gpfs(join(path, 'residuals'))
        src = join(self.getpath, 'residuals')
        dst = join(path, 'residuals', self.getname)
        unix.mv(src, dst)

    def export_traces(self, path, prefix='traces/obs'):
        unix.mkdir_gpfs(join(path, 'traces'))
        src = join(self.getpath, prefix)
        dst = join(path, 'traces', self.getname)
        unix.cp(src, dst)


    ### setup utilities

    def initialize_solver_directories(self):
        """ Creates directory structure expected by SPECFEM2D, copies 
          executables, and prepares input files. Executables must be supplied 
          by user as there is currently no mechanism to automatically compile 
          from source.
        """
        unix.mkdir(self.getpath)
        unix.cd(self.getpath)

        # create directory structure
        unix.mkdir('bin')
        unix.mkdir('DATA')

        unix.mkdir('traces/obs')
        unix.mkdir('traces/syn')
        unix.mkdir('traces/adj')

        unix.mkdir(self.model_databases)

        # copy exectuables
        src = glob(PATH.SOLVER_BINARIES +'/'+ '*')
        dst = 'bin/'
        unix.cp(src, dst)

        # copy input files
        src = glob(PATH.SOLVER_FILES +'/'+ '*')
        dst = 'DATA/'
        unix.cp(src, dst)

        src = 'DATA/SOURCE_' + self.getname
        dst = 'DATA/SOURCE'
        unix.cp(src, dst)

        setpar('f0', PAR.F0, 'DATA/SOURCE')


    ### input file writers

    def write_parameters(self):
        unix.cd(self.getpath)
        solvertools.write_parameters(vars(PAR))

    def write_receivers(self):
        unix.cd(self.getpath)
        key = 'use_existing_STATIONS'
        val = '.true.'
        setpar(key, val)
        _, h = preprocess.load('traces/obs')
        solvertools.write_receivers(h.nr, h.rx, h.rz)

    def write_sources(self):
        unix.cd(self.getpath)
        _, h = preprocess.load(dir='traces/obs')
        solvertools.write_sources(vars(PAR), h)


    ### utility functions

    def mpirun(self, script, output='/dev/null'):
        """ Wrapper for mpirun
        """
        with open(output,'w') as f:
            subprocess.call(
                script,
                shell=True,
                stdout=f)

    ### miscellaneous

    @property
    def data_wildcard(self):
        return glob('OUTPUT_FILES/U?_file_single.su')

    @property
    def model_databases(self):
        return join(self.getpath, 'OUTPUT_FILES/DATABASES_MPI')

    @property
    def source_prefix(self):
        return 'SOURCE'


def loadbyproc(filename, key, nproc=None):
    # read text file
    M = np.loadtxt(filename)
    nrow = M.shape[0]
    ncol = M.shape[1]

    if ncol == 5:
        ioff = 0
    elif ncol == 6:
        ioff = 1
    else:
        raise Exception('Bad SPECFEM2D model or kernel.')

    if key == 'x':
        return M[:, ioff+0]
    elif key == 'z':
        return M[:, ioff+1]
    elif key == 'rho':
        return M[:, ioff+2]
    elif key == 'vp':
        return M[:, ioff+3]
    elif key == 'vs':
        return M[:, ioff+4]



