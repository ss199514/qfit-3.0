import os.path
from numbers import Real
from struct import unpack as _unpack, pack as _pack
from sys import byteorder as _BYTEORDER

import numpy as np

from .spacegroups import GetSpaceGroup, SpaceGroup, SymOpFromString
from .unitcell import UnitCell
from ._extensions import extend_to_p1


class _BaseVolume:

    def __init__(self, array, voxelspacing=1.0, origin=None, dimensions=None):

        self.array = array
        if dimensions is not None:
            voxelspacing = [d / n for d, n in zip(dimensions, reversed(array.shape))]
        elif isinstance(voxelspacing, Real):
            voxelspacing = tuple([voxelspacing] * 3)
        self.voxelspacing = voxelspacing
        self.origin = origin
        if origin is None:
            self.origin = (0, 0, 0)

    @property
    def shape(self):
        return self.array.shape

    def tofile(self, fid, fmt=None):
        if fmt is None:
            fmt = os.path.splitext(fid)[-1][1:]
        if fmt in ('ccp4', 'map', 'mrc'):
            to_mrc(fid, self)
        else:
            raise ValueError("Format is not supported.")


class Volume(_BaseVolume):

    """A rectangular cuboid with a grid. The grid can be anisotropic."""

    @classmethod
    def fromfile(cls, fid, fmt=None):
        p = parse_volume(fid)
        return cls(p.density, voxelspacing=p.voxelspacing,
                origin=p.origin)

    @classmethod
    def zeros(cls, shape, voxelspacing=1.0, origin=(0, 0, 0)):
        array = np.zeros(shape, dtype=np.float32)
        return cls(array, voxelspacing, origin)

    @classmethod
    def zeros_like(cls, volume):
        array = np.zeros_like(volume.array)
        return cls(array, tuple(volume.voxelspacing), tuple(volume.origin))

    def copy(self):
        return Volume(self.array.copy(), voxelspacing=tuple(self.voxelspacing),
                      origin=tuple(self.origin))


class XMap(_BaseVolume):

    """A crystallographic volume with a unit cell."""

    def __init__(self, array, voxelspacing=1.0, origin=None, dimensions=None,
                 unit_cell=None, offset=None, resolution=None, resolution_min=None, hkl=None):
        super().__init__(array, voxelspacing, origin, dimensions)

        self.unit_cell = unit_cell
        self.offset = offset
        self.hkl = hkl
        if offset is None:
            self.offset = (0, 0, 0)
        self.resolution = resolution
        self.resolution_min = resolution_min

    @classmethod
    def fromfile(cls, fname, fmt=None, resolution=None, label="FWT,PHWT"):
        if fmt is None:
            fmt = os.path.splitext(fname)[1]
        if fmt == '.ccp4':
            parser = CCP4Parser(fname)
            a, b, c = parser.abc
            alpha, beta, gamma = parser.angles
            spacegroup = parser.spacegroup
            cell_shape = parser.cell_shape
            unit_cell = UnitCell(a, b, c, alpha, beta, gamma, spacegroup, cell_shape)
            offset = parser.offset
            array = parser.density
            voxelspacing = parser.voxelspacing
            origin = parser.origin
            xmap = cls(array, voxelspacing=voxelspacing, origin=origin,
                   unit_cell=unit_cell, offset=offset, resolution=resolution)
        elif fmt == '.mtz':
            from .mtzfile import MTZFile
            from .transformer import SFTransformer
            mtz = MTZFile(fname)
            hkl = np.asarray(list(zip(mtz['H'], mtz['K'], mtz['L'])), int)
            hkl_base = mtz['HKL_base']
            uc_par = [getattr(hkl_base, x) for x in 'a b c alpha beta gamma'.split()]
            unit_cell = UnitCell(*uc_par)
            try:
                space_group = GetSpaceGroup(mtz.ispg)
            except ValueError:
                symops = [SymOpFromString(string) for string in mtz.symops]
                space_group = SpaceGroup(
                    number=mtz.symi['ispg'],
                    num_sym_equiv=mtz.symi['nsym'],
                    num_primitive_sym_equiv=mtz.symi['nsymp'],
                    short_name=mtz.symi['spgname'],
                    point_group_name=mtz.symi['pgname'],
                    crystal_system=mtz.symi['symtyp'],
                    pdb_name=mtz.symi['spgname'],
                    symop_list=symops,
                )
            unit_cell.space_group = space_group
            f, phi = label.split(',')
            t = SFTransformer(hkl, mtz[f], mtz[phi], unit_cell, space_group)
            grid = t()
            unit_cell.shape = grid.shape
            abc = [getattr(unit_cell, x) for x in 'a b c'.split()]
            voxelspacing = [x / n for x, n in zip(abc, grid.shape[::-1])]
            resolution_min = 1 / np.sqrt(mtz.resmin)
            resolution = 1 / np.sqrt(mtz.resmax)
            xmap = cls(grid, voxelspacing=voxelspacing, unit_cell=unit_cell,
                       resolution=resolution, resolution_min=resolution_min, hkl=hkl)
        else:
            raise RuntimeError("File format not recognized.")
        return xmap

    @classmethod
    def zeros_like(cls, xmap):
        array = np.zeros_like(xmap.array)
        return cls(array, voxelspacing=xmap.voxelspacing, origin=xmap.origin,
                   unit_cell=xmap.unit_cell, offset=xmap.offset, hkl=xmap.hkl,
                   resolution=xmap.resolution, resolution_min=xmap.resolution_min)

    def asymmetric_unit_cell(self):
        raise NotImplementedError

    def canonical_unit_cell(self):
        array = np.zeros(self.unit_cell.shape, np.float32)
        out = XMap(array, voxelspacing=self.voxelspacing, unit_cell=self.unit_cell,
                  hkl=self.hkl, resolution=self.resolution, resolution_min=self.resolution_min)
        offset = np.asarray(self.offset, np.int32)
        for symop in self.unit_cell.space_group.symop_list:
            trans = np.hstack((symop.R, symop.t.reshape(3, -1)))
            trans[:, -1] *= out.shape[::-1]
            extend_to_p1(self.array, offset, trans, out.array)
        return out

    def extract(self, orth_coor, padding=3):
        grid_coor = np.dot(orth_coor, self.unit_cell.orth_to_frac.T)
        grid_coor -= self.offset
        grid_coor *= self.voxelspacing
        uc = self.unit_cell
        abc = np.asarray([uc.a, uc.b, uc.c])
        grid_padding = padding / abc * self.voxelspacing
        lb = grid_coor.min(axis=0) - grid_padding
        ru = grid_coor.max(axis=0) + grid_padding
        lb = np.floor(lb).astype(int)
        ru = np.ceil(ru).astype(int)
        array = self.array[lb[2]:ru[2], lb[1]:ru[1], lb[0]:ru[0]]
        return XMap(array, voxelspacing=self.voxelspacing, origin=self.origin,
                    unit_cell=self.unit_cell, offset=self.offset + lb, resolution=self.resolution,
                    resolution_min=self.resolution_min, hkl=self.hkl)

    def interpolate(self, xyz):
        raise NotImplementedError
        offset = np.asarray(self.offset) * self.voxelspacing
        offset += np.asarray(self.origin)
        grid_xyz = xyz - offset


    def set_space_group(self, space_group):
        self.unit_cell.space_group = GetSpaceGroup(space_group)


# Volume parsers
def parse_volume(fid, fmt=None):
    try:
        fname = fid.name
    except AttributeError:
        fname = fid

    if fmt is None:
        fmt = os.path.splitext(fname)[-1][1:]
    if fmt in ('ccp4', 'map'):
        p = CCP4Parser(fname)
    elif fmt == 'mrc':
        p = MRCParser(fname)
    else:
        raise ValueError('Extension of file is not supported.')
    return p


class CCP4Parser:

    HEADER_SIZE = 1024
    HEADER_TYPE = ('i' * 10 + 'f' * 6 + 'i' * 3 + 'f' * 3 + 'i' * 3 +
                   'f' * 27 + 'c' * 8 + 'f' * 1 + 'i' * 1 + 'c' * 800)
    HEADER_FIELDS = (
          'nc nr ns mode ncstart nrstart nsstart nx ny nz xlength ylength '
          'zlength alpha beta gamma mapc mapr maps amin amax amean ispg '
          'nsymbt lskflg skwmat skwtrn extra xstart ystart zstart map '
          'machst rms nlabel label'
          ).split()
    HEADER_CHUNKS = [1] * 25 + [9, 3, 12] + [1] * 3 + [4, 4, 1, 1, 800]

    def __init__(self, fid):

        if isinstance(fid, str):
            fhandle = open(fid, 'rb')
        elif isinstance(fid, file):
            fhandle = fid
        else:
            raise ValueError("Input should either be a file or filename.")

        self.fhandle = fhandle
        self.fname = fhandle.name

        # first determine the endiannes of the file
        self._get_endiannes()
        # get the header
        self._get_header()
        self.abc = tuple(self.header[key] for key in ('xlength', 'ylength', 'zlength'))
        self.angles = tuple(self.header[key] for key in ('alpha', 'beta', 'gamma'))
        self.shape = tuple(self.header[key] for key in ('nx', 'ny', 'nz'))
        self.voxelspacing = tuple(length / n for length, n in zip(self.abc, self.shape))
        self.spacegroup = int(self.header['ispg'])
        self.cell_shape = [self.header[key] for key in 'nz ny nx'.split()]
        self._get_offset()
        self._get_origin()
        # Get the symbol table and ultimately the density
        self._get_symbt()
        self._get_density()
        self.fhandle.close()


    def _get_endiannes(self):
        self.fhandle.seek(212)
        b = self.fhandle.read(1)

        m_stamp = hex(ord(b))
        if m_stamp == '0x44':
            endian = '<'
        elif m_stamp == '0x11':
            endian = '>'
        else:
            raise ValueError('Endiannes is not properly set in file. Check the file format.')
        self._endian = endian
        self.fhandle.seek(0)

    def _get_header(self):
        header = _unpack(self._endian + self.HEADER_TYPE,
                         self.fhandle.read(self.HEADER_SIZE))
        self.header = {}
        index = 0
        for field, nchunks in zip(self.HEADER_FIELDS, self.HEADER_CHUNKS):
            end = index + nchunks
            if nchunks > 1:
                self.header[field] = header[index: end]
            else:
                self.header[field] = header[index]
            index = end
        self.header['label'] = ''.join(x.decode('utf-8') for x in self.header['label'])

    def _get_offset(self):
        self.offset = [0] * 3
        self.offset[self.header['mapc'] - 1] = self.header['ncstart']
        self.offset[self.header['mapr'] - 1] = self.header['nrstart']
        self.offset[self.header['maps'] - 1] = self.header['nsstart']

    def _get_origin(self):
        self.origin = (0, 0, 0)

    def _get_symbt(self):
        self.symbt = self.fhandle.read(self.header['nsymbt'])

    def _get_density(self):

        # Determine the dtype of the file based on the mode
        mode = self.header['mode']
        if mode == 0:
            dtype = 'i1'
        elif mode == 1:
            dtype = 'i2'
        elif mode == 2:
            dtype = 'f4'

        # Read the density
        storage_shape = tuple(self.header[key] for key in ('ns', 'nr', 'nc'))
        self.density = np.fromfile(self.fhandle,
                              dtype=self._endian + dtype).reshape(storage_shape)

        # Reorder axis so that nx is fastest changing.
        maps, mapr, mapc = [self.header[key] for key in ('maps', 'mapr', 'mapc')]
        if maps == 3 and mapr == 2 and mapc == 1:
            pass
        elif maps == 3 and mapr == 1 and mapc == 2:
            self.density = np.swapaxes(self.density, 1, 2)
        elif maps == 2 and mapr == 1 and mapc == 3:
            self.density = np.swapaxes(self.density, 1, 2)
            self.density = np.swapaxes(self.density, 1, 0)
        elif maps == 1 and mapr == 2 and mapc == 3:
            self.density = np.swapaxes(self.density, 0, 2)
        else:
            raise NotImplementedError("Density storage order ({:} {:} {:}) not supported.".format(maps, mapr, mapc))
        self.density = np.ascontiguousarray(self.density, dtype=np.float32)


class MRCParser(CCP4Parser):

    def _get_origin(self):
        origin_fields = 'xstart ystart zstart'.split()
        origin = [self.header[field] for field in origin_fields]
        return origin


def to_mrc(fid, volume, labels=[], fmt=None):

    if fmt is None:
        fmt = os.path.splitext(fid)[-1][1:]

    if fmt not in ('ccp4', 'mrc', 'map'):
        raise ValueError('Format is not recognized. Use ccp4, mrc, or map.')

    dtype = volume.array.dtype.name
    if dtype == 'int8':
        mode = 0
    elif dtype in ('int16', 'int32'):
        mode = 1
    elif dtype in ('float32', 'float64'):
        mode = 2
    else:
        raise TypeError("Data type ({:})is not supported.".format(dtype))

    if fmt == 'ccp4':
        nxstart, nystart, nzstart = volume.offset
        origin = [0, 0, 0]
        uc = volume.unit_cell
        xl, yl, zl = uc.a, uc.b, uc.c
        alpha, beta, gamma = uc.alpha, uc.beta, uc.gamma
        ispg = uc.space_group.number
        ns, nr, nc = uc.shape
    elif fmt in ('mrc', 'map'):
        nxstart, nystart, nzstart = [0, 0, 0]
        origin = volume.origin
        xl, yl, zl = [vs * n for vs, n in zip(volume.voxelspacing, reversed(volume.shape))]
        alpha = beta = gamma = 90
        ispg = 1
        ns, nr, nc = volume.shape

    voxelspacing = volume.voxelspacing
    nz, ny, nx = volume.shape
    mapc, mapr, maps = [1, 2, 3]
    nsymbt = 0
    lskflg = 0
    skwmat = [0.0] * 9
    skwtrn = [0.0] * 3
    fut_use = [0.0] * 12
    str_map = list('MAP ')
    str_map = 'MAP '
    if _BYTEORDER == 'little':
        machst = list('\x44\x41\x00\x00')
    elif _BYTEORDER == 'big':
        machst = list('\x44\x41\x00\x00')
    else:
        raise ValueError("Byteorder {:} is not recognized".format(byteorder))
    labels = [' '] * 800
    nlabels = 0
    min_density = volume.array.min()
    max_density = volume.array.max()
    mean_density = volume.array.mean()
    std_density = volume.array.std()

    with open(fid, 'wb') as out:
        out.write(_pack('i', nx))
        out.write(_pack('i', ny))
        out.write(_pack('i', nz))
        out.write(_pack('i', mode))
        out.write(_pack('i', nxstart))
        out.write(_pack('i', nystart))
        out.write(_pack('i', nzstart))
        out.write(_pack('i', nc))
        out.write(_pack('i', nr))
        out.write(_pack('i', ns))
        out.write(_pack('f', xl))
        out.write(_pack('f', yl))
        out.write(_pack('f', zl))
        out.write(_pack('f', alpha))
        out.write(_pack('f', beta))
        out.write(_pack('f', gamma))
        out.write(_pack('i', mapc))
        out.write(_pack('i', mapr))
        out.write(_pack('i', maps))
        out.write(_pack('f', min_density))
        out.write(_pack('f', max_density))
        out.write(_pack('f', mean_density))
        out.write(_pack('i', ispg))
        out.write(_pack('i', nsymbt))
        out.write(_pack('i', lskflg))
        for f in skwmat:
            out.write(_pack('f', f))
        for f in skwtrn:
            out.write(_pack('f', f))
        for f in fut_use:
            out.write(_pack('f', f))
        for f in origin:
            out.write(_pack('f', f))
        for c in str_map:
            out.write(_pack('c', c.encode('ascii')))
        for c in machst:
            out.write(_pack('c', c.encode('ascii')))
        out.write(_pack('f', std_density))
        # max 10 labels
        # nlabels = min(len(labels), 10)
        # TODO labels not handled correctly
        #for label in labels:
        #     list_label = [c for c in label]
        #     llabel = len(list_label)
        #     if llabel < 80:
        #
        #     # max 80 characters
        #     label = min(len(label), 80)
        out.write(_pack('i', nlabels))
        for c in labels:
            out.write(_pack('c', c.encode('ascii')))
        # write density
        modes = [np.int8, np.int16, np.float32]
        volume.array.astype(modes[mode]).tofile(out)
