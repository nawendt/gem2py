"""Tools to process GEMPAK-formatted products."""

import bisect
import contextlib
import ctypes
import math
import struct
import sys
from datetime import datetime, timedelta
from enum import Enum
from itertools import product

import numpy as np

import pyproj

import xarray as xr

from .gemcalc import (interp_logp_data, interp_logp_height,
                      interp_logp_pressure, interp_moist_height)
from .tools import IOBuffer, NamedStruct, open_as_needed


ANLB_SIZE = 128
BYTES_PER_WORD = 4
NAVB_SIZE = 256
PARAM_ATTR = [('name', (4, 's')), ('scale', (1, 'i')),
              ('offset', (1, 'i')), ('bits', (1, 'i'))]
USED_FLAG = 9999
UNUSED_FLAG = -9999

GEMPROJ_TO_PROJ = {
    'MER': ('merc', 'cyl'),
    'NPS': ('stere', 'azm'),
    'SPS': ('stere', 'azm'),
    'LCC': ('lcc', 'con'),
    'SCC': ('lcc', 'con'),
    'CED': ('eqc', 'cyl'),
    'MCD': ('eqc', 'cyl'),
    'NOR': ('ortho', 'azm'),
    'SOR': ('ortho', 'azm'),
    'STR': ('stere', 'azm'),
    'AED': ('aeqd', 'azm'),
    'ORT': ('ortho', 'azm'),
    'LEA': ('laea', 'azm'),
    'GNO': ('gnom', 'azm'),
}

GVCORD_TO_VAR = {
    'PRES': 'p',
    'HGHT': 'z',
    'THTA': 'theta',
}


class FileTypes(Enum):
    """GEMPAK file type."""

    surface = 1
    sounding = 2
    grid = 3


class DataTypes(Enum):
    """Data management library data types."""

    real = 1
    integer = 2
    character = 3
    realpack = 4
    grid = 5


class VerticalCoordinates(Enum):
    """Veritical coordinates."""

    none = 0
    pres = 1
    thta = 2
    hght = 3
    sgma = 4
    dpth = 5
    hybd = 6
    pvab = 7
    pvbl = 8


class PackingType(Enum):
    """GRIB packing type."""

    none = 0
    grib = 1
    nmc = 2
    diff = 3
    dec = 4
    grib2 = 5


class ForecastType(Enum):
    """Forecast type."""

    analysis = 0
    forecast = 1
    guess = 2
    initial = 3


class DataSource(Enum):
    """Data source."""

    model = 0
    airway_surface = 1
    metar = 2
    ship = 3
    raob_buoy = 4
    synop_raob_vas = 5
    grid = 6
    watch_by_county = 7
    unknown = 99
    text = 100
    raob = 104


GEMPAK_HEADER = 'GEMPAK DATA MANAGEMENT FILE '


def _word_to_position(word, bytes_per_word=BYTES_PER_WORD):
    """Return beginning position of a word in bytes."""
    return (word * bytes_per_word) - bytes_per_word


class GempakFile():
    """Base class for GEMPAK files.

    Reads ubiquitous GEMPAK file headers (i.e., the data managment portion of
    each file).
    """

    prod_desc_fmt = [('version', 'i'), ('file_headers', 'i'),
                     ('file_keys_ptr', 'i'), ('rows', 'i'),
                     ('row_keys', 'i'), ('row_keys_ptr', 'i'),
                     ('row_headers_ptr', 'i'), ('columns', 'i'),
                     ('column_keys', 'i'), ('column_keys_ptr', 'i'),
                     ('column_headers_ptr', 'i'), ('parts', 'i'),
                     ('parts_ptr', 'i'), ('data_mgmt_ptr', 'i'),
                     ('data_mgmt_length', 'i'), ('data_block_ptr', 'i'),
                     ('file_type', 'i', FileTypes),
                     ('data_source', 'i', DataSource),
                     ('machine_type', 'i'), ('missing_int', 'i'),
                     (None, '12x'), ('missing_float', 'f')]

    grid_nav_fmt = [('grid_definition_type', 'f'),
                    ('projection', '3sx', bytes.decode),
                    ('left_grid_number', 'f'), ('bottom_grid_number', 'f'),
                    ('right_grid_number', 'f'), ('top_grid_number', 'f'),
                    ('lower_left_lat', 'f'), ('lower_left_lon', 'f'),
                    ('upper_right_lat', 'f'), ('upper_right_lon', 'f'),
                    ('proj_angle1', 'f'), ('proj_angle2', 'f'),
                    ('proj_angle3', 'f'), (None, '972x')]

    grid_anl_fmt1 = [('analysis_type', 'f'), ('delta_n', 'f'),
                     ('delta_x', 'f'), ('delta_y', 'f'),
                     (None, '4x'), ('garea_llcr_lat', 'f'),
                     ('garea_llcr_lon', 'f'), ('garea_urcr_lat', 'f'),
                     ('garea_urcr_lon', 'f'), ('extarea_llcr_lat', 'f'),
                     ('extarea_llcr_lon', 'f'), ('extarea_urcr_lat', 'f'),
                     ('extarea_urcr_lon', 'f'), ('datarea_llcr_lat', 'f'),
                     ('datarea_llcr_lon', 'f'), ('datarea_urcr_lat', 'f'),
                     ('datarea_urcrn_lon', 'f'), (None, '444x')]

    grid_anl_fmt2 = [('analysis_type', 'f'), ('delta_n', 'f'),
                     ('grid_ext_left', 'f'), ('grid_ext_down', 'f'),
                     ('grid_ext_right', 'f'), ('grid_ext_up', 'f'),
                     ('garea_llcr_lat', 'f'), ('garea_llcr_lon', 'f'),
                     ('garea_urcr_lat', 'f'), ('garea_urcr_lon', 'f'),
                     ('extarea_llcr_lat', 'f'), ('extarea_llcr_lon', 'f'),
                     ('extarea_urcr_lat', 'f'), ('extarea_urcr_lon', 'f'),
                     ('datarea_llcr_lat', 'f'), ('datarea_llcr_lon', 'f'),
                     ('datarea_urcr_lat', 'f'), ('datarea_urcrn_lon', 'f'),
                     (None, '440x')]

    data_management_fmt = ([('next_free_word', 'i'), ('max_free_pairs', 'i'),
                           ('actual_free_pairs', 'i'), ('last_word', 'i')]
                           + [('free_word{:d}'.format(n), 'i') for n in range(1, 29)])

    def __init__(self, file):
        """Instantiate GempakFile object from file."""
        fobj = open_as_needed(file)

        with contextlib.closing(fobj):
            self._buffer = IOBuffer.fromfile(fobj)

        # Save file start position as pointers use this as reference
        self._start = self._buffer.set_mark()

        # Process the main GEMPAK header to verify file format
        self._process_gempak_header()
        meta = self._buffer.set_mark()

        # # Check for byte swapping
        self._swap_bytes(bytes(self._buffer.read_binary(4)))
        self._buffer.jump_to(meta)

        # Process main metadata header
        self.prod_desc = self._buffer.read_struct(NamedStruct(self.prod_desc_fmt,
                                                              self.prefmt,
                                                              'ProductDescription'))

        # File Keys
        # Surface and upper-air files will not have the file headers, so we need to check.
        if self.prod_desc.file_headers > 0:
            # This would grab any file headers, but NAVB and ANLB are the only ones used.
            fkey_prod = product(['header_name', 'header_length', 'header_type'],
                                range(1, self.prod_desc.file_headers + 1))
            fkey_names = ['{}{}'.format(*x) for x in fkey_prod]
            fkey_info = list(zip(fkey_names, np.repeat(('4s', 'i', 'i'),
                                                       self.prod_desc.file_headers)))
            self.file_keys_format = NamedStruct(fkey_info, self.prefmt, 'FileKeys')

            self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.file_keys_ptr))
            self.file_keys = self._buffer.read_struct(self.file_keys_format)

            # file_key_blocks = self._buffer.set_mark()
            # Navigation Block
            navb_size = self._buffer.read_int(4, self.endian, False)
            if navb_size != NAVB_SIZE:
                raise ValueError('Navigation block size does not match GEMPAK specification')
            else:
                self.navigation_block = (
                    self._buffer.read_struct(NamedStruct(self.grid_nav_fmt,
                                                         self.prefmt,
                                                         'NavigationBlock'))
                )
            self.kx = int(self.navigation_block.right_grid_number)
            self.ky = int(self.navigation_block.top_grid_number)

            # Analysis Block
            anlb_size = self._buffer.read_int(4, self.endian, False)
            anlb_start = self._buffer.set_mark()
            if anlb_size != ANLB_SIZE:
                raise ValueError('Analysis block size does not match GEMPAK specification')
            else:
                anlb_type = self._buffer.read_struct(struct.Struct(self.prefmt + 'f'))[0]
                self._buffer.jump_to(anlb_start)
                if anlb_type == 1:
                    self.analysis_block = (
                        self._buffer.read_struct(NamedStruct(self.grid_anl_fmt1,
                                                             self.prefmt,
                                                             'AnalysisBlock'))
                    )
                elif anlb_type == 2:
                    self.analysis_block = (
                        self._buffer.read_struct(NamedStruct(self.grid_anl_fmt2,
                                                             self.prefmt,
                                                             'AnalysisBlock'))
                    )
                else:
                    self.analysis_block = None
        else:
            self.analysis_block = None
            self.navigation_block = None

        # Data Management
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.data_mgmt_ptr))
        self.data_management = self._buffer.read_struct(NamedStruct(self.data_management_fmt,
                                                                    self.prefmt,
                                                                    'DataManagement'))

        # Row Keys
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.row_keys_ptr))
        row_key_info = [('row_key{:d}'.format(n), '4s', self._decode_strip)
                        for n in range(1, self.prod_desc.row_keys + 1)]
        row_key_info.extend([(None, None)])
        row_keys_fmt = NamedStruct(row_key_info, self.prefmt, 'RowKeys')
        self.row_keys = self._buffer.read_struct(row_keys_fmt)

        # Column Keys
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.column_keys_ptr))
        column_key_info = [('column_key{:d}'.format(n), '4s', self._decode_strip)
                           for n in range(1, self.prod_desc.column_keys + 1)]
        column_key_info.extend([(None, None)])
        column_keys_fmt = NamedStruct(column_key_info, self.prefmt, 'ColumnKeys')
        self.column_keys = self._buffer.read_struct(column_keys_fmt)

        # Parts
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.parts_ptr))
        # parts = self._buffer.set_mark()
        self.parts = []
        parts_info = [('name', '4s', self._decode_strip),
                      (None, '{:d}x'.format((self.prod_desc.parts - 1) * BYTES_PER_WORD)),
                      ('header_length', 'i'),
                      (None, '{:d}x'.format((self.prod_desc.parts - 1) * BYTES_PER_WORD)),
                      ('data_type', 'i', DataTypes),
                      (None, '{:d}x'.format((self.prod_desc.parts - 1) * BYTES_PER_WORD)),
                      ('parameter_count', 'i')]
        parts_info.extend([(None, None)])
        parts_fmt = NamedStruct(parts_info, self.prefmt, 'Parts')
        for n in range(1, self.prod_desc.parts + 1):
            self.parts.append(self._buffer.read_struct(parts_fmt))
            self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.parts_ptr + n))

        # Parameters
        # No need to jump to any position as this follows parts information
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.parts_ptr
                                                            + self.prod_desc.parts * 4))
        self.parameters = [{key: [] for key, _ in PARAM_ATTR}
                           for n in range(self.prod_desc.parts)]
        for attr, fmt in PARAM_ATTR:
            fmt = (fmt[0], self.prefmt + fmt[1])
            for n, part in enumerate(self.parts):
                for _ in range(part.parameter_count):
                    if fmt[1] == 's':
                        self.parameters[n][attr] += [self._buffer.read_binary(*fmt)[0].decode()]  # noqa: E501
                    else:
                        self.parameters[n][attr] += self._buffer.read_binary(*fmt)

    def _swap_bytes(self, binary):
        self.swaped_bytes = (struct.pack('@i', 1) != binary)

        if self.swaped_bytes:
            if sys.byteorder == 'little':
                self.prefmt = '>'
                self.endian = 'big'
            elif sys.byteorder == 'big':
                self.prefmt = '<'
                self.endian = 'little'
        else:
            self.prefmt = ''
            self.endian = sys.byteorder

    def _process_gempak_header(self):
        """Read the GEMPAK header from the file, if necessary."""
        fmt = [('text', '28s', bytes.decode), (None, None)]

        header = self._buffer.read_struct(NamedStruct(fmt, '', 'GempakHeader'))
        if header.text != GEMPAK_HEADER:
            raise TypeError('Unknown file format or invalid GEMPAK file')

    @staticmethod
    def _convert_dattim(dattim):
        if dattim:
            if dattim < 100000000:
                dt = datetime.strptime(str(dattim), '%y%m%d')
            else:
                dt = datetime.strptime('{:010d}'.format(dattim), '%m%d%y%H%M')
        else:
            dt = None
        return dt

    @staticmethod
    def _convert_ftime(ftime):
        if ftime:
            iftype = ForecastType(ftime // 100000)
            iftime = ftime - iftype.value * 100000
            hours = iftime // 100
            minutes = iftime - hours * 100
            out = (iftype.name, timedelta(hours=hours, minutes=minutes))
        else:
            out = None
        return out

    @staticmethod
    def _convert_level(level):
        if level and level >= 0:
            return level
        else:
            return None

    @staticmethod
    def _convert_vertical_coord(coord):
        if coord:
            if coord <= 8:
                return VerticalCoordinates(coord).name.upper()
            else:
                return struct.pack('i', coord).decode()
        else:
            return None

    @staticmethod
    def _convert_parms(parm):
        dparm = parm.decode()
        return dparm.strip() if dparm.strip() else None

    @staticmethod
    def _fortran_ishift(i, shift):
        mask = 0xffffffff
        if shift > 0:
            shifted = ctypes.c_int32(i << shift).value
        elif shift < 0:
            if i < 0:
                shifted = (i & mask) >> abs(shift)
            else:
                shifted = i >> abs(shift)
        elif shift == 0:
            shifted = i
        else:
            raise ValueError('Bad shift value {}.'.format(shift))
        return shifted

    @staticmethod
    def _decode_strip(b):
        return b.decode().strip()

    @staticmethod
    def _make_date(dattim):
        return GempakFile._convert_dattim(dattim).date()

    @staticmethod
    def _make_time(t):
        string = '{:04d}'.format(t)
        return datetime.strptime(string, '%H%M').time()

    def _unpack_real(self, buffer, parameters, length):
        """Unpack floating point data packed in integers.

        Similar to DP_UNPK subroutine in GEMPAK.
        """
        nparms = len(parameters['name'])
        mskpat = 0xffffffff

        pwords = (sum(parameters['bits']) - 1) // 32 + 1
        npack = (length - 1) // pwords + 1
        unpacked = np.ones(npack * nparms) * self.prod_desc.missing_float
        if npack * pwords != length:
            raise ValueError('Unpacking length mismatch.')

        ir = 0
        ii = 0
        for _i in range(npack):
            pdat = buffer[ii:(ii + pwords)]
            rdat = unpacked[ir:(ir + nparms)]
            itotal = 0
            for idata in range(nparms):
                scale = 10**parameters['scale'][idata]
                offset = parameters['offset'][idata]
                bits = parameters['bits'][idata]
                isbitc = (itotal % 32) + 1
                iswrdc = (itotal // 32)
                imissc = self._fortran_ishift(mskpat, bits - 32)

                jbit = bits
                jsbit = isbitc
                jshift = 1 - jsbit
                jsword = iswrdc
                jword = pdat[jsword]
                mask = self._fortran_ishift(mskpat, jbit - 32)
                ifield = self._fortran_ishift(jword, jshift)
                ifield &= mask

                if (jsbit + jbit - 1) > 32:
                    jword = pdat[jsword + 1]
                    jshift += 32
                    iword = self._fortran_ishift(jword, jshift)
                    iword &= mask
                    ifield |= iword

                if ifield == imissc:
                    rdat[idata] = self.prod_desc.missing_float
                else:
                    rdat[idata] = (ifield + offset) * scale
                itotal += bits
            unpacked[ir:(ir + nparms)] = rdat
            ir += nparms
            ii += pwords

        return unpacked.tolist()


class GempakGrid(GempakFile):
    """Subclass of GempakFile specific to GEMPAK gridded data."""

    def __init__(self, file, *args, **kwargs):
        super().__init__(file)

        datetime_names = ['GDT1', 'GDT2']
        level_names = ['GLV1', 'GLV2']
        ftime_names = ['GTM1', 'GTM2']
        string_names = ['GPM1', 'GPM2', 'GPM3']

        # Row Headers
        # Based on GEMPAK source, row/col headers have a 0th element in their Fortran arrays.
        # This appears to be a flag value to say a header is used or not. 9999
        # means its in use, otherwise -9999. GEMPAK allows empty grids, etc., but
        # no real need to keep track of that in Python.
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.row_headers_ptr))
        self.row_headers = []
        row_headers_info = [(key, 'i') for key in self.row_keys]
        row_headers_info.extend([(None, None)])
        row_headers_fmt = NamedStruct(row_headers_info, self.prefmt, 'RowHeaders')
        for _ in range(1, self.prod_desc.rows + 1):
            if self._buffer.read_int(4, self.endian, False) == USED_FLAG:
                self.row_headers.append(self._buffer.read_struct(row_headers_fmt))

        # Column Headers
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.column_headers_ptr))
        self.column_headers = []
        column_headers_info = [(key, 'i', self._convert_level) if key in level_names
                               else (key, 'i', self._convert_vertical_coord) if key == 'GVCD'
                               else (key, 'i', self._convert_dattim) if key in datetime_names
                               else (key, 'i', self._convert_ftime) if key in ftime_names
                               else (key, '4s', self._convert_parms) if key in string_names
                               else (key, 'i')
                               for key in self.column_keys]
        column_headers_info.extend([(None, None)])
        column_headers_fmt = NamedStruct(column_headers_info, self.prefmt, 'ColumnHeaders')
        for _ in range(1, self.prod_desc.columns + 1):
            if self._buffer.read_int(4, self.endian, False) == USED_FLAG:
                self.column_headers.append(self._buffer.read_struct(column_headers_fmt))

        # Coordinates
        if self.navigation_block is not None:
            self._get_crs()
            self._get_coordinates()

    def _get_crs(self):
        gemproj = self.navigation_block.projection
        proj, ptype = GEMPROJ_TO_PROJ[gemproj]

        if ptype == 'azm':
            lat_0 = self.navigation_block.proj_angle1
            lon_0 = self.navigation_block.proj_angle2
            lat_ts = self.navigation_block.proj_angle3
            self.crs = pyproj.CRS.from_dict({'proj': proj,
                                             'lat_0': lat_0,
                                             'lon_0': lon_0,
                                             'lat_ts': lat_ts})
        elif ptype == 'cyl':
            if gemproj != 'mcd':
                lat_0 = self.navigation_block.proj_angle1
                lon_0 = self.navigation_block.proj_angle2
                lat_ts = self.navigation_block.proj_angle3
                self.crs = pyproj.CRS.from_dict({'proj': proj,
                                                 'lat_0': lat_0,
                                                 'lon_0': lon_0,
                                                 'lat_ts': lat_ts})
            else:
                avglat = (self.navigation_block.upper_right_lat
                          + self.navigation_block.lower_left_lat) * 0.5
                k_0 = (1 / math.cos(avglat)
                       if self.navigation_block.proj_angle1 == 0
                       else self.navigation_block.proj_angle1
                       )
                lon_0 = self.navigation_block.proj_angle2
                self.crs = pyproj.CRS.from_dict({'proj': proj,
                                                 'lat_0': avglat,
                                                 'lon_0': lon_0,
                                                 'k_0': k_0})
        elif ptype == 'con':
            lat_1 = self.navigation_block.proj_angle1
            lon_0 = self.navigation_block.proj_angle2
            lat_2 = self.navigation_block.proj_angle3
            self.crs = pyproj.CRS.from_dict({'proj': proj,
                                             'lon_0': lon_0,
                                             'lat_1': lat_1,
                                             'lat_2': lat_2})

    def _get_coordinates(self):
        transform = pyproj.Proj(self.crs)
        llx, lly = transform(self.navigation_block.lower_left_lon,
                             self.navigation_block.lower_left_lat)
        urx, ury = transform(self.navigation_block.upper_right_lon,
                             self.navigation_block.upper_right_lat)
        self.x = np.linspace(llx, urx, self.kx)
        self.y = np.linspace(lly, ury, self.ky)
        xx, yy = np.meshgrid(self.x, self.y)
        self.lon, self.lat = transform(xx, yy, inverse=True)

    def _unpack_grid(self, packing_type, part):
        if packing_type == PackingType.none:
            lendat = self.data_header_length - part.header_length - 1

            if lendat > 1:
                buffer_fmt = '{}{}f'.format(self.prefmt, lendat)
                buffer = self._buffer.read_struct(struct.Struct(buffer_fmt))
                grid = np.zeros(self.ky * self.kx)
                grid[...] = buffer
            else:
                grid = None

            return grid

        elif packing_type == PackingType.nmc:
            raise NotImplementedError('NMC unpacking not supported.')
            # integer_meta_fmt = [('bits', 'i'), ('missing_flag', 'i'), ('kxky', 'i')]
            # real_meta_fmt = [('reference', 'f'), ('scale', 'f')]
            # self.grid_meta_int = self._buffer.read_struct(NamedStruct(integer_meta_fmt,
            #                                                           self.prefmt,
            #                                                           'GridMetaInt'))
            # self.grid_meta_real = self._buffer.read_struct(NamedStruct(real_meta_fmt,
            #                                                            self.prefmt,
            #                                                            'GridMetaReal'))
            # grid_start = self._buffer.set_mark()
        elif packing_type == PackingType.diff:
            integer_meta_fmt = [('bits', 'i'), ('missing_flag', 'i'),
                                ('kxky', 'i'), ('kx', 'i')]
            real_meta_fmt = [('reference', 'f'), ('scale', 'f'), ('diffmin', 'f')]
            self.grid_meta_int = self._buffer.read_struct(NamedStruct(integer_meta_fmt,
                                                                      self.prefmt,
                                                                      'GridMetaInt'))
            self.grid_meta_real = self._buffer.read_struct(NamedStruct(real_meta_fmt,
                                                                       self.prefmt,
                                                                       'GridMetaReal'))
            # grid_start = self._buffer.set_mark()

            imiss = 2**self.grid_meta_int.bits - 1
            lendat = self.data_header_length - part.header_length - 8
            packed_buffer_fmt = '{}{}i'.format(self.prefmt, lendat)
            packed_buffer = self._buffer.read_struct(struct.Struct(packed_buffer_fmt))
            grid = np.zeros((self.ky, self.kx))

            if lendat > 1:
                iword = 0
                ibit = 1
                first = True
                for j in range(self.ky):
                    line = False
                    for i in range(self.kx):
                        jshft = self.grid_meta_int.bits + ibit - 33
                        idat = self._fortran_ishift(packed_buffer[iword], jshft)
                        idat &= imiss

                        if jshft > 0:
                            jshft -= 32
                            idat2 = self._fortran_ishift(packed_buffer[iword + 1], jshft)
                            idat |= idat2

                        ibit += self.grid_meta_int.bits
                        if ibit > 32:
                            ibit -= 32
                            iword += 1

                        if (self.grid_meta_int.missing_flag and idat == imiss):
                            grid[j, i] = self.prod_desc.missing_float
                        else:
                            if first:
                                grid[j, i] = self.grid_meta_real.reference
                                psav = self.grid_meta_real.reference
                                plin = self.grid_meta_real.reference
                                line = True
                                first = False
                            else:
                                if not line:
                                    grid[j, i] = plin + (self.grid_meta_real.diffmin
                                                         + idat * self.grid_meta_real.scale)
                                    line = True
                                    plin = grid[j, i]
                                else:
                                    grid[j, i] = psav + (self.grid_meta_real.diffmin
                                                         + idat * self.grid_meta_real.scale)
                                psav = grid[j, i]
            else:
                grid = None

            return grid

        elif packing_type in [PackingType.grib, PackingType.dec]:
            integer_meta_fmt = [('bits', 'i'), ('missing_flag', 'i'), ('kxky', 'i')]
            real_meta_fmt = [('reference', 'f'), ('scale', 'f')]
            self.grid_meta_int = self._buffer.read_struct(NamedStruct(integer_meta_fmt,
                                                                      self.prefmt,
                                                                      'GridMetaInt'))
            self.grid_meta_real = self._buffer.read_struct(NamedStruct(real_meta_fmt,
                                                                       self.prefmt,
                                                                       'GridMetaReal'))
            # grid_start = self._buffer.set_mark()

            lendat = self.data_header_length - part.header_length - 6
            packed_buffer_fmt = '{}{}i'.format(self.prefmt, lendat)

            grid = np.zeros(self.grid_meta_int.kxky)
            packed_buffer = self._buffer.read_struct(struct.Struct(packed_buffer_fmt))
            if lendat > 1:
                imax = 2**self.grid_meta_int.bits - 1
                ibit = 1
                iword = 0
                for cell in range(self.grid_meta_int.kxky):
                    jshft = self.grid_meta_int.bits + ibit - 33
                    idat = self._fortran_ishift(packed_buffer[iword], jshft)
                    idat &= imax

                    if jshft > 0:
                        jshft -= 32
                        idat2 = self._fortran_ishift(packed_buffer[iword + 1], jshft)
                        idat |= idat2

                    if (idat == imax) and self.grid_meta_int.missing_flag:
                        grid[cell] = self.prod_desc.missing_float
                    else:
                        grid[cell] = (self.grid_meta_real.reference
                                      + (idat * self.grid_meta_real.scale))

                    ibit += self.grid_meta_int.bits
                    if ibit > 32:
                        ibit -= 32
                        iword += 1
            else:
                grid = None

            return grid
        elif packing_type == PackingType.grib2:
            raise NotImplementedError('GRIB2 unpacking not supported.')
            # integer_meta_fmt = [('iuscal', 'i'), ('kx', 'i'),
            #                     ('ky', 'i'), ('iscan_mode', 'i')]
            # real_meta_fmt = [('rmsval', 'f')]
            # self.grid_meta_int = self._buffer.read_struct(NamedStruct(integer_meta_fmt,
            #                                                           self.prefmt,
            #                                                           'GridMetaInt'))
            # self.grid_meta_real = self._buffer.read_struct(NamedStruct(real_meta_fmt,
            #                                                            self.prefmt,
            #                                                            'GridMetaReal'))
            # grid_start = self._buffer.set_mark()
        else:
            raise NotImplementedError('No method for unknown grid packing {}'
                                      .format(packing_type.name))

    def to_xarray(self):
        """Output GempakGrids as a list of xarry DataArrays."""
        grids = []
        for icol, col_head in enumerate(self.column_headers):
            for irow, _row_head in enumerate(self.row_headers):
                for iprt, part in enumerate(self.parts):
                    pointer = (self.prod_desc.data_block_ptr
                               + (irow * self.prod_desc.columns * self.prod_desc.parts)
                               + (icol * self.prod_desc.parts + iprt))
                    self._buffer.jump_to(self._start, _word_to_position(pointer))
                    self.data_ptr = self._buffer.read_int(4, self.endian, False)
                    self._buffer.jump_to(self._start, _word_to_position(self.data_ptr))
                    self.data_header_length = self._buffer.read_int(4, self.endian, False)
                    data_header = self._buffer.set_mark()
                    self._buffer.jump_to(data_header,
                                         _word_to_position(part.header_length + 1))
                    packing_type = PackingType(self._buffer.read_int(4, self.endian, False))

                    ftype, ftime = col_head.GTM1
                    init = col_head.GDT1
                    valid = init + ftime
                    gvcord = col_head.GVCD.lower() if col_head.GVCD is not None else 'none'
                    var = (GVCORD_TO_VAR[col_head.GPM1]
                           if col_head.GPM1 in GVCORD_TO_VAR
                           else col_head.GPM1.lower()
                           )
                    data = self._unpack_grid(packing_type, part)
                    if data is not None:
                        if data.ndim < 2:
                            data = np.ma.array(data.reshape((self.ky, self.kx)),
                                               mask=data == self.prod_desc.missing_float)
                        else:
                            data = np.ma.array(data, mask=data == self.prod_desc.missing_float)

                        xrda = xr.DataArray(
                            data=data[np.newaxis, np.newaxis, ...],
                            coords={
                                'time': [valid],
                                gvcord: [col_head.GLV1],
                                'x': self.x,
                                'y': self.y,
                            },
                            dims=['time', gvcord, 'y', 'x'],
                            name=var,
                            attrs=self.crs.to_cf(),
                        )
                        grids.append(xrda)

                    else:
                        ('Bad grid for %s', col_head.GPM1)
        return grids


class GempakSounding(GempakFile):
    """Subclass of GempakFile specific to GEMPAK sounding data."""

    def __init__(self, file, *args, **kwargs):
        super().__init__(file)

        # Row Headers
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.row_headers_ptr))
        self.row_headers = []
        row_headers_info = [(key, 'i', self._make_date) if key == 'DATE'
                            else (key, 'i', self._make_time) if key == 'TIME'
                            else (key, 'i')
                            for key in self.row_keys]
        row_headers_info.extend([(None, None)])
        row_headers_fmt = NamedStruct(row_headers_info, self.prefmt, 'RowHeaders')
        for _ in range(1, self.prod_desc.rows + 1):
            if self._buffer.read_int(4, self.endian, False) == USED_FLAG:
                self.row_headers.append(self._buffer.read_struct(row_headers_fmt))

        # Column Headers
        self._buffer.jump_to(self._start, _word_to_position(self.prod_desc.column_headers_ptr))
        self.column_headers = []
        column_headers_info = [(key, '4s', self._decode_strip) if key == 'STID'
                               else (key, 'i') if key == 'STNM'
                               else (key, 'i', lambda x: x / 100) if key == 'SLAT'
                               else (key, 'i', lambda x: x / 100) if key == 'SLON'
                               else (key, 'i') if key == 'SELV'
                               else (key, '4s', self._decode_strip) if key == 'STAT'
                               else (key, '4s', self._decode_strip) if key == 'COUN'
                               else (key, '4s', self._decode_strip) if key == 'STD2'
                               else (key, 'i')
                               for key in self.column_keys]
        column_headers_info.extend([(None, None)])
        column_headers_fmt = NamedStruct(column_headers_info, self.prefmt, 'ColumnHeaders')
        for _ in range(1, self.prod_desc.columns + 1):
            if self._buffer.read_int(4, self.endian, False) == USED_FLAG:
                self.column_headers.append(self._buffer.read_struct(column_headers_fmt))

        self.merged = 'SNDT' in (part.name for part in self.parts)

    def _unpack_merged(self):
        soundings = []
        for irow, row_head in enumerate(self.row_headers):
            for icol, col_head in enumerate(self.column_headers):
                sounding = {'STID': col_head.STID,
                            'STNM': col_head.STNM,
                            'SLAT': col_head.SLAT,
                            'SLON': col_head.SLON,
                            'SELV': col_head.SELV,
                            'DATE': row_head.DATE,
                            'TIME': row_head.TIME,
                            }
                for iprt, part in enumerate(self.parts):
                    pointer = (self.prod_desc.data_block_ptr
                               + (irow * self.prod_desc.columns * self.prod_desc.parts)
                               + (icol * self.prod_desc.parts + iprt))
                    self._buffer.jump_to(self._start, _word_to_position(pointer))
                    self.data_ptr = self._buffer.read_int(4, self.endian, False)
                    if not self.data_ptr:
                        continue
                    self._buffer.jump_to(self._start, _word_to_position(self.data_ptr))
                    self.data_header_length = self._buffer.read_int(4, self.endian, False)
                    data_header = self._buffer.set_mark()
                    self._buffer.jump_to(data_header,
                                         _word_to_position(part.header_length + 1))
                    lendat = self.data_header_length - part.header_length

                    if part.data_type == DataTypes.real:
                        packed_buffer_fmt = '{}{}f'.format(self.prefmt, lendat)
                        packed_buffer = (
                            self._buffer.read_struct(struct.Struct(packed_buffer_fmt))
                        )
                    elif part.data_type == DataTypes.realpack:
                        packed_buffer_fmt = '{}{}i'.format(self.prefmt, lendat)
                        packed_buffer = (
                            self._buffer.read_struct(struct.Struct(packed_buffer_fmt))
                        )
                    else:
                        raise NotImplementedError('No methods for data type {}'
                                                  .format(part.data_type))

                    parameters = self.parameters[iprt]
                    nparms = len(parameters['name'])

                    if part.data_type == DataTypes.realpack:
                        unpacked = self._unpack_real(packed_buffer, parameters, lendat)
                        for iprm, param in enumerate(parameters['name']):
                            sounding[param] = unpacked[iprm::nparms]
                    else:
                        for iprm, param in enumerate(parameters['name']):
                            sounding[param] = packed_buffer[iprm::nparms]

                soundings.append(sounding)
        return soundings

    def _unpack_unmerged(self):
        soundings = []
        for irow, row_head in enumerate(self.row_headers):
            for icol, col_head in enumerate(self.column_headers):
                sounding = {'STID': col_head.STID,
                            'STNM': col_head.STNM,
                            'SLAT': col_head.SLAT,
                            'SLON': col_head.SLON,
                            'SELV': col_head.SELV,
                            'DATE': row_head.DATE,
                            'TIME': row_head.TIME,
                            }
                for iprt, part in enumerate(self.parts):
                    pointer = (self.prod_desc.data_block_ptr
                               + (irow * self.prod_desc.columns * self.prod_desc.parts)
                               + (icol * self.prod_desc.parts + iprt))
                    self._buffer.jump_to(self._start, _word_to_position(pointer))
                    self.data_ptr = self._buffer.read_int(4, self.endian, False)
                    if not self.data_ptr:
                        continue
                    self._buffer.jump_to(self._start, _word_to_position(self.data_ptr))
                    self.data_header_length = self._buffer.read_int(4, self.endian, False)
                    data_header = self._buffer.set_mark()
                    self._buffer.jump_to(data_header,
                                         _word_to_position(part.header_length + 1))
                    lendat = self.data_header_length - part.header_length

                    if part.data_type == DataTypes.real:
                        packed_buffer_fmt = '{}{}f'.format(self.prefmt, lendat)
                        packed_buffer = (
                            self._buffer.read_struct(struct.Struct(packed_buffer_fmt))
                        )
                    elif part.data_type == DataTypes.realpack:
                        packed_buffer_fmt = '{}{}i'.format(self.prefmt, lendat)
                        packed_buffer = (
                            self._buffer.read_struct(struct.Struct(packed_buffer_fmt))
                        )
                    else:
                        raise NotImplementedError('No methods for data type {}'
                                                  .format(part.data_type))

                    parameters = self.parameters[iprt]
                    nparms = len(parameters['name'])
                    sounding[part.name] = {}
                    for iprm, param in enumerate(parameters['name']):
                        if part.data_type == DataTypes.realpack:
                            # According to SNPRMF GEMPAK documentation, unmerged data
                            # should not have this type of packing. This exception will
                            # be kept here just in case that is incorrect.
                            raise NotImplementedError('No method to unpack unmerged '
                                                      'sounding integers.')
                        else:
                            sounding[part.name][param] = packed_buffer[iprm::nparms]

                soundings.append(self._merge_sounding(sounding))
        return soundings

    def _merge_sounding(self, parts):
        merged = {'STID': parts['STID'],
                  'STNM': parts['STNM'],
                  'SLAT': parts['SLAT'],
                  'SLON': parts['SLON'],
                  'SELV': parts['SELV'],
                  'DATE': parts['DATE'],
                  'TIME': parts['TIME'],
                  'PRES': [],
                  'HGHT': [],
                  'TEMP': [],
                  'DWPT': [],
                  'DRCT': [],
                  'SPED': [],
                  }

        # Number of parameter levels
        num_man_levels = len(parts['TTAA']['PRES']) if 'TTAA' in parts else 0
        num_man_wind_levels = len(parts['PPAA']['PRES']) if 'PPAA' in parts else 0
        num_trop_levels = len(parts['TRPA']['PRES']) if 'TRPA' in parts else 0
        num_max_wind_levels = len(parts['MXWA']['PRES']) if 'MXWA' in parts else 0
        num_sigt_levels = len(parts['TTBB']['PRES']) if 'TTBB' in parts else 0
        num_sigw_levels = len(parts['PPBB']['SPED']) if 'PPBB' in parts else 0
        num_above_man_levels = len(parts['TTCC']['PRES']) if 'TTCC' in parts else 0
        num_above_trop_levels = len(parts['TRPC']['PRES']) if 'TRPC' in parts else 0
        num_above_max_wind_levels = len(parts['MXWC']['SPED']) if 'MXWC' in parts else 0
        num_above_sigt_levels = len(parts['TTDD']['PRES']) if 'TTDD' in parts else 0
        num_above_sigw_levels = len(parts['PPDD']['SPED']) if 'PPDD' in parts else 0
        num_above_man_wind_levels = len(parts['PPCC']['SPED']) if 'PPCC' in parts else 0

        # Check SIG wind vertical coordinate
        ppbb_is_z = 'HGHT' in parts['PPBB']
        ppdd_is_z = 'HGHT' in parts['PPDD']

        # Process surface data
        if num_man_levels < 1:
            merged['PRES'].append(self.prod_desc.missing_float)
            merged['HGHT'].append(self.prod_desc.missing_float)
            merged['TEMP'].append(self.prod_desc.missing_float)
            merged['DWPT'].append(self.prod_desc.missing_float)
            merged['DRCT'].append(self.prod_desc.missing_float)
            merged['SPED'].append(self.prod_desc.missing_float)
        else:
            merged['PRES'].append(parts['TTAA']['PRES'][0])
            merged['HGHT'].append(parts['TTAA']['HGHT'][0])
            merged['TEMP'].append(parts['TTAA']['TEMP'][0])
            merged['DWPT'].append(parts['TTAA']['DWPT'][0])
            merged['DRCT'].append(parts['TTAA']['DRCT'][0])
            merged['SPED'].append(parts['TTAA']['SPED'][0])

        merged['HGHT'][0] = merged['SELV']

        first_man_p = self.prod_desc.missing_float
        for mp, mt, mz in zip(parts['TTAA']['PRES'],
                              parts['TTAA']['PRES'],
                              parts['TTAA']['PRES']):
            if (mp != self.prod_desc.missing_float
               and mt != self.prod_desc.missing_float
               and mz != self.prod_desc.missing_float):
                first_man_p = mp
                break

        surface_p = merged['PRES'][0]
        if surface_p > 1060:
            surface_p = self.prod_desc.missing_float

        if (surface_p == self.prod_desc.missing_float
           or (surface_p < first_man_p
               and surface_p != self.prod_desc.missing_float)):
            merged['PRES'][0] = self.prod_desc.missing_float
            merged['HGHT'][0] = self.prod_desc.missing_float
            merged['TEMP'][0] = self.prod_desc.missing_float
            merged['DWPT'][0] = self.prod_desc.missing_float
            merged['DRCT'][0] = self.prod_desc.missing_float
            merged['SPED'][0] = self.prod_desc.missing_float

        if (num_above_sigt_levels >= 1
           and parts['TTBB']['PRES'][0] != self.prod_desc.missing_float
           and parts['TTBB']['TEMP'][0] != self.prod_desc.missing_float):
            first_man_p = merged['PRES'][0]
            first_sig_p = parts['TTBB']['PRES'][0]
            if (first_man_p == self.prod_desc.missing_float
               or np.isclose(first_man_p, first_sig_p)):
                merged['PRES'][0] = parts['TTBB']['PRES'][0]
                merged['DWPT'][0] = parts['TTBB']['DWPT'][0]
                merged['TEMP'][0] = parts['TTBB']['TEMP'][0]

        if ppbb_is_z:
            if (num_above_sigw_levels >= 1
               and parts['PPBB']['HGHT'][0] == 0
               and parts['PPBB']['DRCT'][0] != self.prod_desc.missing_float):
                merged['DRCT'][0] = parts['PPBB']['DRCT'][0]
                merged['SPED'][0] = parts['PPBB']['SPED'][0]
        else:
            if (num_above_sigw_levels >= 1
               and parts['PPBB']['PRES'][0] != self.prod_desc.missing_float
               and parts['PPBB']['DRCT'][0] != self.prod_desc.missing_float):
                first_man_p = merged['PRES'][0]
                first_sig_p = abs(parts['PPBB']['PRES'][0])
                if (first_man_p == self.prod_desc.missing_float
                   or np.isclose(first_man_p, first_sig_p)):
                    merged['DRCT'][0] = abs(parts['PPBB']['PRES'][0])
                    merged['DRCT'][0] = parts['PPBB']['DRCT'][0]
                    merged['SPED'][0] = parts['PPBB']['SPED'][0]

        # Merge MAN temperature
        bgl = 0
        if num_man_levels >= 2 or num_above_man_levels >= 1:
            if merged['PRES'][0] == self.prod_desc.missing_float:
                plast = 2000
            else:
                plast = merged['PRES'][0]

            for i in range(1, num_man_levels):
                if (parts['TTAA']['PRES'][i] < plast
                   and parts['TTAA']['PRES'][i] != self.prod_desc.missing_float
                   and parts['TTAA']['TEMP'][i] != self.prod_desc.missing_float
                   and parts['TTAA']['HGHT'][i] != self.prod_desc.missing_float):
                    for pname, pval in parts['TTAA'].items():
                        merged[pname].append(pval[i])
                    plast = merged['PRES'][-1]
                else:
                    bgl += 1

            for i in range(1, num_above_man_levels):
                if (parts['TTCC']['PRES'][i] < plast
                   and parts['TTCC']['PRES'][i] != self.prod_desc.missing_float
                   and parts['TTCC']['TEMP'][i] != self.prod_desc.missing_float
                   and parts['TTCC']['HGHT'][i] != self.prod_desc.missing_float):
                    for pname, pval in parts['TTCC'].items():
                        merged[pname].append(pval[i])
                    plast = merged['PRES'][-1]

        # Merge MAN wind
        if num_man_wind_levels >= 1 and num_man_levels >= 2:
            for iwind, pres in enumerate(parts['PPAA']['PRES']):
                if pres in merged['PRES'][1:]:
                    loc = merged['PRES'].index(pres)
                    if merged['DRCT'][loc] == self.prod_desc.missing_float:
                        merged['DRCT'][loc] = parts['PPAA']['DRCT'][iwind]
                        merged['SPED'][loc] = parts['PPAA']['SPED'][iwind]
                else:
                    size = len(merged['PRES'])
                    loc = size - bisect.bisect_left(merged['PRES'][1:][::-1], pres)
                    if loc >= size + 1:
                        loc = -1
                    merged['PRES'].insert(loc, pres)
                    merged['DRCT'].insert(loc, parts['PPAA']['DRCT'][iwind])
                    merged['SPED'].insert(loc, parts['PPAA']['SPED'][iwind])

        if num_above_man_wind_levels >= 1 and num_man_levels >= 2:
            for iwind, pres in enumerate(parts['PPCC']['PRES']):
                if pres in merged['PRES'][1:]:
                    loc = merged['PRES'].index(pres)
                    if merged['DRCT'][loc] == self.prod_desc.missing_float:
                        merged['DRCT'][loc] = parts['PPCC']['DRCT'][iwind]
                        merged['SPED'][loc] = parts['PPCC']['SPED'][iwind]
                else:
                    size = len(merged['PRES'])
                    loc = size - bisect.bisect_left(merged['PRES'][1:][::-1], pres)
                    if loc >= size + 1:
                        loc = -1
                    merged['PRES'].insert(loc, pres)
                    merged['DRCT'].insert(loc, parts['PPCC']['DRCT'][iwind])
                    merged['SPED'].insert(loc, parts['PPCC']['SPED'][iwind])

        # Merge TROP
        if num_trop_levels >= 1 or num_above_trop_levels >= 1:
            if merged['PRES'][0] != self.prod_desc.missing_float:
                pbot = merged['PRES'][0]
            elif len(merged['PRES']) > 1:
                pbot = merged['PRES'][1]
                if pbot < parts['TRPA']['PRES'][1]:
                    pbot = 1050
            else:
                pbot = 1050

        if num_trop_levels >= 1:
            for itrp, pres in enumerate(parts['TRPA']['PRES']):
                pres = abs(pres)
                if (pres != self.prod_desc.missing_float
                   and parts['TRPA']['TEMP'][itrp] != self.prod_desc.missing_float
                   and pres != 0):
                    if pres > pbot:
                        continue
                    elif pres in merged['PRES']:
                        ploc = merged['PRES'].index(pres)
                        if merged['TEMP'][ploc] == self.prod_desc.missing_float:
                            merged['TEMP'][ploc] = parts['TRPA']['TEMP'][itrp]
                            merged['DWPT'][ploc] = parts['TRPA']['DWPT'][itrp]
                        if merged['DRCT'][ploc] == self.prod_desc.missing_float:
                            merged['DRCT'][ploc] = parts['TRPA']['DRCT'][itrp]
                            merged['SPED'][ploc] = parts['TRPA']['SPED'][itrp]
                        merged['HGHT'][ploc] = self.prod_desc.missing_float
                    else:
                        size = len(merged['PRES'])
                        loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                        merged['PRES'].insert(loc, pres)
                        merged['TEMP'].insert(loc, parts['TRPA']['TEMP'][itrp])
                        merged['DWPT'].insert(loc, parts['TRPA']['DWPT'][itrp])
                        merged['DRCT'].insert(loc, parts['TRPA']['DRCT'][itrp])
                        merged['SPED'].insert(loc, parts['TRPA']['SPED'][itrp])
                        merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                pbot = pres

        if num_above_trop_levels >= 1:
            for itrp, pres in enumerate(parts['TRPC']['PRES']):
                pres = abs(pres)
                if (pres != self.prod_desc.missing_float
                   and parts['TRPC']['TEMP'][itrp] != self.prod_desc.missing_float
                   and pres != 0):
                    if pres > pbot:
                        continue
                    elif pres in merged['PRES']:
                        ploc = merged['PRES'].index(pres)
                        if merged['TEMP'][ploc] == self.prod_desc.missing_float:
                            merged['TEMP'][ploc] = parts['TRPC']['TEMP'][itrp]
                            merged['DWPT'][ploc] = parts['TRPC']['DWPT'][itrp]
                        if merged['DRCT'][ploc] == self.prod_desc.missing_float:
                            merged['DRCT'][ploc] = parts['TRPC']['DRCT'][itrp]
                            merged['SPED'][ploc] = parts['TRPC']['SPED'][itrp]
                        merged['HGHT'][ploc] = self.prod_desc.missing_float
                    else:
                        size = len(merged['PRES'])
                        loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                        merged['PRES'].insert(loc, pres)
                        merged['TEMP'].insert(loc, parts['TRPC']['TEMP'][itrp])
                        merged['DWPT'].insert(loc, parts['TRPC']['DWPT'][itrp])
                        merged['DRCT'].insert(loc, parts['TRPC']['DRCT'][itrp])
                        merged['SPED'].insert(loc, parts['TRPC']['SPED'][itrp])
                        merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                pbot = pres

        # Merge SIG temperature
        if num_sigt_levels >= 1 or num_above_sigt_levels >= 1:
            if merged['PRES'][0] != self.prod_desc.missing_float:
                pbot = merged['PRES'][0]
            elif len(merged['PRES']) > 1:
                pbot = merged['PRES'][1]
                if pbot < parts['TTBB']['PRES'][1]:
                    pbot = 1050
            else:
                pbot = 1050

        if num_sigt_levels >= 1:
            for isigt, pres in enumerate(parts['TTBB']['PRES']):
                pres = abs(pres)
                if (pres != self.prod_desc.missing_float
                   and parts['TTBB']['TEMP'][isigt] != self.prod_desc.missing_float
                   and pres != 0):
                    if pres > pbot:
                        continue
                    elif pres in merged['PRES']:
                        ploc = merged['PRES'].index(pres)
                        if merged['TEMP'][ploc] == self.prod_desc.missing_float:
                            merged['TEMP'][ploc] = parts['TTBB']['TEMP'][isigt]
                            merged['DWPT'][ploc] = parts['TTBB']['DWPT'][isigt]
                    else:
                        size = len(merged['PRES'])
                        loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                        merged['PRES'].insert(loc, pres)
                        merged['TEMP'].insert(loc, parts['TTBB']['TEMP'][isigt])
                        merged['DWPT'].insert(loc, parts['TTBB']['DWPT'][isigt])
                        merged['DRCT'].insert(loc, self.prod_desc.missing_float)
                        merged['SPED'].insert(loc, self.prod_desc.missing_float)
                        merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                pbot = pres

        if num_above_sigt_levels >= 1:
            for isigt, pres in enumerate(parts['TTDD']['PRES']):
                pres = abs(pres)
                if (pres != self.prod_desc.missing_float
                   and parts['TTDD']['TEMP'][isigt] != self.prod_desc.missing_float
                   and pres != 0):
                    if pres > pbot:
                        continue
                    elif pres in merged['PRES']:
                        ploc = merged['PRES'].index(pres)
                        if merged['TEMP'][ploc] == self.prod_desc.missing_float:
                            merged['TEMP'][ploc] = parts['TTDD']['TEMP'][isigt]
                            merged['DWPT'][ploc] = parts['TTDD']['DWPT'][isigt]
                        merged['DRCT'][ploc] = self.prod_desc.missing_float
                        merged['SPED'][ploc] = self.prod_desc.missing_float
                        merged['HGHT'][ploc] = self.prod_desc.missing_float
                    else:
                        size = len(merged['PRES'])
                        loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                        merged['PRES'].insert(loc, pres)
                        merged['TEMP'].insert(loc, parts['TTDD']['TEMP'][isigt])
                        merged['DWPT'].insert(loc, parts['TTDD']['DWPT'][isigt])
                        merged['DRCT'].insert(loc, self.prod_desc.missing_float)
                        merged['SPED'].insert(loc, self.prod_desc.missing_float)
                        merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                pbot = pres

        # Interpolate heights
        interp_moist_height(merged, self.prod_desc.missing_float)

        # Merge SIG winds on pressure surfaces
        if not ppbb_is_z or not ppdd_is_z:
            if num_sigw_levels >= 1 or num_above_sigw_levels >= 1:
                if merged['PRES'][0] != self.prod_desc.missing_float:
                    pbot = merged['PRES'][0]
                elif len(merged['PRES']) > 1:
                    pbot = merged['PRES'][1]
                else:
                    pbot = 0

            if num_sigw_levels >= 1 and not ppbb_is_z:
                for isigw, pres in enumerate(parts['PPBB']['PRES']):
                    pres = abs(pres)
                    if (pres != self.prod_desc.missing_float
                       and parts['PPBB']['DRCT'][isigw] != self.prod_desc.missing_float
                       and parts['PPBB']['SPED'][isigw] != self.prod_desc.missing_float
                       and pres != 0):
                        if pres > pbot:
                            continue
                        elif pres in merged['PRES']:
                            ploc = merged['PRES'].index(pres)
                            if (merged['DRCT'][ploc] == self.prod_desc.missing_float
                               or merged['SPED'][ploc] == self.prod_desc.missing_float):
                                merged['DRCT'][ploc] = parts['PPBB']['DRCT'][isigw]
                                merged['SPED'][ploc] = parts['PPBB']['SPED'][isigw]
                        else:
                            size = len(merged['PRES'])
                            loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                            merged['PRES'].insert(loc, pres)
                            merged['DRCT'].insert(loc, parts['PPBB']['DRCT'][isigw])
                            merged['SPED'].insert(loc, parts['PPBB']['SPED'][isigw])
                            merged['TEMP'].insert(loc, self.prod_desc.missing_float)
                            merged['DWPT'].insert(loc, self.prod_desc.missing_float)
                            merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                    pbot = pres

            if num_above_sigw_levels >= 1 and not ppdd_is_z:
                for isigw, pres in enumerate(parts['PPDD']['PRES']):
                    pres = abs(pres)
                    if (pres != self.prod_desc.missing_float
                       and parts['PPDD']['DRCT'][isigw] != self.prod_desc.missing_float
                       and parts['PPDD']['SPED'][isigw] != self.prod_desc.missing_float
                       and pres != 0):
                        if pres > pbot:
                            continue
                        elif pres in merged['PRES']:
                            ploc = merged['PRES'].index(pres)
                            if (merged['DRCT'][ploc] == self.prod_desc.missing_float
                               or merged['SPED'][ploc] == self.prod_desc.missing_float):
                                merged['DRCT'][ploc] = parts['PPDD']['DRCT'][isigw]
                                merged['SPED'][ploc] = parts['PPDD']['SPED'][isigw]
                        else:
                            size = len(merged['PRES'])
                            loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                            merged['PRES'].insert(loc, pres)
                            merged['DRCT'].insert(loc, parts['PPDD']['DRCT'][isigw])
                            merged['SPED'].insert(loc, parts['PPDD']['SPED'][isigw])
                            merged['TEMP'].insert(loc, self.prod_desc.missing_float)
                            merged['DWPT'].insert(loc, self.prod_desc.missing_float)
                            merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                    pbot = pres

        # Merge max winds on pressure surfaces
        if num_max_wind_levels >= 1 or num_above_max_wind_levels >= 1:
            if merged['PRES'][0] != self.prod_desc.missing_float:
                pbot = merged['PRES'][0]
            elif len(merged['PRES']) > 1:
                pbot = merged['PRES'][1]
            else:
                pbot = 0

        if num_max_wind_levels >= 1:
            for imxw, pres in enumerate(parts['MXWA']['PRES']):
                pres = abs(pres)
                if (pres != self.prod_desc.missing_float
                   and parts['MXWA']['DRCT'][imxw] != self.prod_desc.missing_float
                   and parts['MXWA']['SPED'][imxw] != self.prod_desc.missing_float
                   and pres != 0):
                    if pres > pbot:
                        continue
                    elif pres in merged['PRES']:
                        ploc = merged['PRES'].index(pres)
                        if (merged['DRCT'][ploc] == self.prod_desc.missing_float
                           or merged['SPED'][ploc] == self.prod_desc.missing_float):
                            merged['DRCT'][ploc] = parts['MXWA']['DRCT'][imxw]
                            merged['SPED'][ploc] = parts['MXWA']['SPED'][imxw]
                    else:
                        size = len(merged['PRES'])
                        loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                        merged['PRES'].insert(loc, pres)
                        merged['DRCT'].insert(loc, parts['MXWA']['DRCT'][imxw])
                        merged['SPED'].insert(loc, parts['MXWA']['SPED'][imxw])
                        merged['TEMP'].insert(loc, self.prod_desc.missing_float)
                        merged['DWPT'].insert(loc, self.prod_desc.missing_float)
                        merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                pbot = pres

        if num_above_max_wind_levels >= 1:
            for imxw, pres in enumerate(parts['MXWC']['PRES']):
                pres = abs(pres)
                if (pres != self.prod_desc.missing_float
                   and parts['MXWC']['DRCT'][imxw] != self.prod_desc.missing_float
                   and parts['MXWC']['SPED'][imxw] != self.prod_desc.missing_float
                   and pres != 0):
                    if pres > pbot:
                        continue
                    elif pres in merged['PRES']:
                        ploc = merged['PRES'].index(pres)
                        if (merged['DRCT'][ploc] == self.prod_desc.missing_float
                           or merged['SPED'][ploc] == self.prod_desc.missing_float):
                            merged['DRCT'][ploc] = parts['MXWC']['DRCT'][imxw]
                            merged['SPED'][ploc] = parts['MXWC']['SPED'][imxw]
                    else:
                        size = len(merged['PRES'])
                        loc = size - bisect.bisect_left(merged['PRES'][::-1], pres)
                        merged['PRES'].insert(loc, pres)
                        merged['DRCT'].insert(loc, parts['MXWC']['DRCT'][imxw])
                        merged['SPED'].insert(loc, parts['MXWC']['SPED'][imxw])
                        merged['TEMP'].insert(loc, self.prod_desc.missing_float)
                        merged['DWPT'].insert(loc, self.prod_desc.missing_float)
                        merged['HGHT'].insert(loc, self.prod_desc.missing_float)
                pbot = pres

        # Interpolate height for SIG/MAX winds
        interp_logp_height(merged, self.prod_desc.missing_float)

        # Merge SIG winds on height surfaces
        if ppbb_is_z or ppdd_is_z:
            nsgw = num_sigw_levels if ppbb_is_z else 0
            nasw = num_above_sigw_levels if ppdd_is_z else 0
            if ((nsgw >= 1 and parts['PPBB']['HGHT'][0] == 0)
               or parts['PPBB']['HGHT'][0] == merged['HGHT'][0]):
                istart = 1
            else:
                istart = 0

            size = len(merged['HGHT'])
            psfc = merged['PRES'][0]
            zsfc = merged['HGHT'][0]

            if (size >= 2 and psfc != self.prod_desc.missing_float
               and zsfc != self.prod_desc.missing_float):
                more = True
                zold = merged['HGHT'][0]
                znxt = merged['HGHT'][1]
                ilev = 1
            elif size >= 3:
                more = True
                zold = merged['HGHT'][1]
                znxt = merged['HGHT'][2]
                ilev = 2
            else:
                zold = self.prod_desc.missing_float
                znxt = self.prod_desc.missing_float

            if (zold == self.prod_desc.missing_float
               or znxt == self.prod_desc.missing_float):
                more = False

            if istart <= nsgw:
                above = False
                i = istart
                iend = nsgw
            else:
                above = True
                i = 0
                iend = nasw

            while more and i < iend:
                if not above:
                    hght = parts['PPBB']['HGHT'][i]
                    drct = parts['PPBB']['DRCT'][i]
                    sped = parts['PPBB']['SPED'][i]
                else:
                    hght = parts['PPDD']['HGHT'][i]
                    drct = parts['PPDD']['DRCT'][i]
                    sped = parts['PPDD']['SPED'][i]
                skip = False

                if (hght == self.prod_desc.missing_float
                   and drct == self.prod_desc.missing_float
                   and sped == self.prod_desc.missing_float):
                    skip = True
                elif abs(zold - hght) < 1:
                    skip = True
                    if (merged['DRCT'][ilev - 1] == self.prod_desc.missing_float
                       or merged['SPED'][ilev - 1] == self.prod_desc.missing_float):
                        merged['DRCT'][ilev - 1] = drct
                        merged['SPED'][ilev - 1] = sped
                elif hght <= zold:
                    skip = True
                elif hght >= znxt:
                    while more and hght > znxt:
                        zold = znxt
                        ilev += 1
                        if ilev >= size:
                            more = False
                        else:
                            znxt = merged['HGHT'][ilev]
                            if znxt == self.prod_desc.missing_float:
                                more = False

                if more and not skip:
                    if abs(znxt - hght) < 1:
                        if (merged['DRCT'][ilev - 1] == self.prod_desc.missing_float
                           or merged['SPED'][ilev - 1] == self.prod_desc.missing_float):
                            merged['DRCT'][ilev] = drct
                            merged['SPED'][ilev] = sped
                    else:
                        loc = bisect.bisect_left(merged['HGHT'], hght)
                        merged['HGHT'].insert(loc, hght)
                        merged['DRCT'].insert(loc, drct)
                        merged['SPED'].insert(loc, sped)
                        merged['PRES'].insert(loc, self.prod_desc.missing_float)
                        merged['TEMP'].insert(loc, self.prod_desc.missing_float)
                        merged['DWPT'].insert(loc, self.prod_desc.missing_float)
                        size += 1

                if not above and i == nsgw - 1:
                    above = True
                    i = 0
                    iend = nasw
                else:
                    i += 1

        # Interpolate misssing pressure with height
        interp_logp_pressure(merged, self.prod_desc.missing_float)

        # Interpolate missing data
        interp_logp_data(merged, self.prod_desc.missing_float)

        # Add below ground MAN data
        if merged['PRES'][0] != self.prod_desc.missing_float:
            if bgl > 0:
                for ibgl in range(1, num_man_levels):
                    pres = parts['TTAA']['PRES'][ibgl]
                    if pres > merged['PRES'][0]:
                        loc = size - bisect.bisect_left(merged['PRES'][1:][::-1], pres)
                        merged['PRES'].insert(loc, pres)
                        merged['TEMP'].insert(loc, parts['TTAA']['TEMP'][ibgl])
                        merged['DWPT'].insert(loc, parts['TTAA']['DWPT'][ibgl])
                        merged['DRCT'].insert(loc, parts['TTAA']['DRCT'][ibgl])
                        merged['SPED'].insert(loc, parts['TTAA']['SPED'][ibgl])
                        merged['HGHT'].insert(loc, parts['TTAA']['HGHT'][ibgl])
                        size += 1

        return merged

    def to_xarray(self):
        """Output GempakSoundings as a list of xarry Datasets."""
        soundings = []

        if not self.merged:
            data = self._unpack_unmerged()
        else:
            data = self._unpack_merged()

        for snd in data:
            if 'PRES' not in snd:
                continue
            attrs = {
                'station_id': snd.pop('STID'),
                'station_number': snd.pop('STNM'),
                'lat': snd.pop('SLAT'),
                'lon': snd.pop('SLON'),
                'elevation': snd.pop('SELV'),
            }
            dt = datetime.combine(snd.pop('DATE'), snd.pop('TIME'))
            pres = np.array(snd.pop('PRES'))

            var = {}
            for param, values in snd.items():
                values = np.array(values)[np.newaxis, ...]
                maskval = np.ma.array(values, mask=values == self.prod_desc.missing_float)
                var[param.lower()] = (['time', 'pres'], maskval)

            xrds = xr.Dataset(var,
                              coords={'time': np.atleast_1d(dt), 'pres': pres},
                              attrs=attrs)

            soundings.append(xrds)
        return soundings


class GempakSurface(GempakFile):
    """Subclass of GempakFile specific to GEMPAK surface data."""

    pass