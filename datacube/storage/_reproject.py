# coding=utf-8
"""
Create/store dataset data into storage units based on the provided storage mappings.

Important functions are:

* :func:`reproject_and_fuse`

"""
import logging
from contextlib import contextmanager
from pathlib import Path
import numpy as np
from affine import Affine
import rasterio
from rasterio.warp import Resampling
import urllib.parse
from urllib.parse import urlparse
from typing import Union, Optional, Callable, List, Any, Iterator

from datacube.storage import netcdf_writer
from datacube.utils import datetime_to_seconds_since_1970, DatacubeException, ignore_exceptions_if
from datacube.utils import geometry
from datacube.utils.geometry import GeoBox, roi_is_empty
from datacube.utils.math import num2numpy
from datacube.utils import uri_to_local_path, get_part_from_uri
from . import DataSource, GeoRasterReader, RasterShape, RasterWindow, BandInfo

_LOG = logging.getLogger(__name__)

RESAMPLING_METHODS = {r.name: r for r in Resampling}

GDAL_NETCDF_DIM = ('NETCDF_DIM_'
                   if str(rasterio.__gdal_version__) >= '1.10.0' else
                   'NETCDF_DIMENSION_')

FuserFunction = Callable[[np.ndarray, np.ndarray], Any]  # pylint: disable=invalid-name


def _rasterio_crs_wkt(src):
    if src.crs:
        return str(src.crs.wkt)
    else:
        return ''


def reproject_and_fuse(datasources: List[DataSource],
                       destination: np.ndarray,
                       dst_gbox: GeoBox,
                       dst_nodata: Optional[Union[int, float]],
                       resampling: str = 'nearest',
                       fuse_func: Optional[FuserFunction] = None,
                       skip_broken_datasets: bool = False):
    """
    Reproject and fuse `sources` into a 2D numpy array `destination`.

    :param datasources: Data sources to open and read from
    :param destination: ndarray of appropriate size to read data into
    :param dst_gbox: GeoBox defining destination region
    :param skip_broken_datasets: Carry on in the face of adversity and failing reads.
    """
    # pylint: disable=too-many-locals
    from ._read import read_time_slice
    assert len(destination.shape) == 2

    def copyto_fuser(dest: np.ndarray, src: np.ndarray) -> None:
        where_nodata = (dest == dst_nodata) if not np.isnan(dst_nodata) else np.isnan(dest)
        np.copyto(dest, src, where=where_nodata)

    fuse_func = fuse_func or copyto_fuser

    destination.fill(dst_nodata)
    if len(datasources) == 0:
        return destination
    elif len(datasources) == 1:
        with ignore_exceptions_if(skip_broken_datasets):
            with datasources[0].open() as rdr:
                read_time_slice(rdr, destination, dst_gbox, resampling, dst_nodata)

        return destination
    else:
        # Multiple sources, we need to fuse them together into a single array
        buffer_ = np.full(destination.shape, dst_nodata, dtype=destination.dtype)
        for source in datasources:
            with ignore_exceptions_if(skip_broken_datasets):
                with source.open() as rdr:
                    roi = read_time_slice(rdr, buffer_, dst_gbox, resampling, dst_nodata)

                if not roi_is_empty(roi):
                    fuse_func(destination[roi], buffer_[roi])
                    buffer_[roi] = dst_nodata  # clean up for next read

        return destination


class BandDataSource(GeoRasterReader):
    """
    Wrapper for a :class:`rasterio.Band` object

    :type source: rasterio.Band
    """

    def __init__(self, source, nodata=None):
        self.source = source
        if nodata is None:
            nodata = self.source.ds.nodatavals[self.source.bidx-1]

        self._nodata = num2numpy(nodata, source.dtype)

    @property
    def nodata(self):
        return self._nodata

    @property
    def crs(self) -> geometry.CRS:
        return geometry.CRS(_rasterio_crs_wkt(self.source.ds))

    @property
    def transform(self) -> Affine:
        return self.source.ds.transform

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(self.source.dtype)

    @property
    def shape(self) -> RasterShape:
        return self.source.shape

    def read(self, window: Optional[RasterWindow] = None,
             out_shape: Optional[RasterShape] = None) -> Optional[np.ndarray]:
        """Read data in the native format, returning a numpy array
        """
        return self.source.ds.read(indexes=self.source.bidx, window=window, out_shape=out_shape)


class OverrideBandDataSource(GeoRasterReader):
    """Wrapper for a rasterio.Band object that overrides nodata, CRS and transform

    This is useful for files with malformed or missing properties.


    :type source: rasterio.Band
    """

    def __init__(self,
                 source: rasterio.Band,
                 nodata,
                 crs: geometry.CRS,
                 transform: Affine):
        self.source = source
        self._nodata = num2numpy(nodata, source.dtype)
        self._crs = crs
        self._transform = transform

    @property
    def crs(self) -> geometry.CRS:
        return self._crs

    @property
    def transform(self) -> Affine:
        return self._transform

    @property
    def nodata(self):
        return self._nodata

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(self.source.dtype)

    @property
    def shape(self) -> RasterShape:
        return self.source.shape

    def read(self, window: Optional[RasterWindow] = None,
             out_shape: Optional[RasterShape] = None) -> Optional[np.ndarray]:
        """Read data in the native format, returning a native array
        """
        return self.source.ds.read(indexes=self.source.bidx, window=window, out_shape=out_shape)


class RasterioDataSource(DataSource):
    """
    Abstract class used by fuse_sources and :func:`read_from_source`

    """

    def __init__(self, filename, nodata):
        self.filename = filename
        self.nodata = nodata

    def get_bandnumber(self, src):
        raise NotImplementedError()

    def get_transform(self, shape):
        raise NotImplementedError()

    def get_crs(self):
        raise NotImplementedError()

    @contextmanager
    def open(self) -> Iterator[GeoRasterReader]:
        """Context manager which returns a :class:`BandDataSource`"""
        try:
            _LOG.debug("opening %s", self.filename)
            with rasterio.open(self.filename) as src:
                override = False

                transform = src.transform
                if transform.is_identity:
                    override = True
                    transform = self.get_transform(src.shape)

                try:
                    crs = geometry.CRS(_rasterio_crs_wkt(src))
                except ValueError:
                    override = True
                    crs = self.get_crs()

                # The [1.0a1-1.0a8] releases of rasterio had a bug that means it
                # cannot read multiband data into a numpy array during reprojection
                # We override it here to force the reading and reprojection into separate steps
                # TODO: Remove when we no longer care about those versions of rasterio
                bandnumber = self.get_bandnumber(src)
                band = rasterio.band(src, bandnumber)
                nodata = src.nodatavals[band.bidx-1] if src.nodatavals[band.bidx-1] is not None else self.nodata
                nodata = num2numpy(nodata, band.dtype)

                if override:
                    yield OverrideBandDataSource(band, nodata=nodata, crs=crs, transform=transform)
                else:
                    yield BandDataSource(band, nodata=nodata)

        except Exception as e:
            _LOG.error("Error opening source dataset: %s", self.filename)
            raise e


class RasterDatasetDataSource(RasterioDataSource):
    """Data source for reading from a Data Cube Dataset"""

    def __init__(self, band: BandInfo):
        """
        Initialise for reading from a Data Cube Dataset.

        :param dataset: dataset to read from
        :param measurement_id: measurement to read. a single 'band' or 'slice'
        """
        self._band_info = band
        self._netcdf = ('netcdf' in band.format.lower())
        self._part = get_part_from_uri(band.uri)
        filename = _url2rasterio(band.uri, band.format, band.layer)
        super(RasterDatasetDataSource, self).__init__(filename, nodata=band.nodata)

    def get_bandnumber(self, src=None) -> Optional[int]:

        # If `band` property is set to an integer it overrides any other logic
        bi = self._band_info
        if bi.band is not None:
            return bi.band

        if not self._netcdf:
            return 1

        # Netcdf only below
        if self._part is not None:
            return self._part + 1  # Convert to rasterio 1-based indexing

        if src is None:
            # File wasnt' open, could be unstacked file in a new format, or
            # stacked/unstacked in old. We assume caller knows what to do
            # (maybe based on some side-channel information), so just report
            # undefined.
            return None

        if src.count == 1:  # Single-slice netcdf file
            return 1

        _LOG.debug("Encountered stacked netcdf file without recorded index\n - %s", src.name)

        # Below is backwards compatibility code

        tag_name = GDAL_NETCDF_DIM + 'time'
        if tag_name not in src.tags(1):  # TODO: support time-less datasets properly
            return 1

        time = bi.center_time
        sec_since_1970 = datetime_to_seconds_since_1970(time)

        idx = 0
        dist = float('+inf')
        for i in range(1, src.count + 1):
            v = float(src.tags(i)[tag_name])
            if abs(sec_since_1970 - v) < dist:
                idx = i
                dist = abs(sec_since_1970 - v)
        return idx

    def get_transform(self, shape: RasterShape) -> Affine:
        return self._band_info.transform * Affine.scale(1 / shape[1], 1 / shape[0])

    def get_crs(self):
        return self._band_info.crs


def register_scheme(*schemes):
    """
    Register additional uri schemes as supporting relative offsets (etc), so that band/measurement paths can be
    calculated relative to the base uri.
    """
    urllib.parse.uses_netloc.extend(schemes)
    urllib.parse.uses_relative.extend(schemes)
    urllib.parse.uses_params.extend(schemes)


# Not recognised by python by default. Doctests below will fail without it.
register_scheme('s3')


def _url2rasterio(url_str, fmt, layer):
    """
    turn URL into a string that could be passed to raterio.open
    """
    url = urlparse(url_str)
    assert url.scheme, "Expecting URL with scheme here"

    # if format is NETCDF or HDF need to pass NETCDF:path:band as filename to rasterio/GDAL
    for nasty_format in ('netcdf', 'hdf'):
        if nasty_format in fmt.lower():
            if url.scheme != 'file':
                raise RuntimeError("Can't access %s over %s" % (fmt, url.scheme))
            filename = '%s:"%s":%s' % (fmt, uri_to_local_path(url_str), layer)
            return filename

    if url.scheme and url.scheme != 'file':
        return url_str

    # if local path strip scheme and other gunk
    return str(uri_to_local_path(url_str))


def create_netcdf_storage_unit(filename,
                               crs, coordinates, variables, variable_params, global_attributes=None,
                               netcdfparams=None):
    """
    Create a NetCDF file on disk.

    :param pathlib.Path filename: filename to write to
    :param datacube.utils.geometry.CRS crs: Datacube CRS object defining the spatial projection
    :param dict coordinates: Dict of named `datacube.model.Coordinate`s to create
    :param dict variables: Dict of named `datacube.model.Variable`s to create
    :param dict variable_params:
        Dict of dicts, with keys matching variable names, of extra parameters for variables
    :param dict global_attributes: named global attributes to add to output file
    :param dict netcdfparams: Extra parameters to use when creating netcdf file
    :return: open netCDF4.Dataset object, ready for writing to
    """
    filename = Path(filename)
    if filename.exists():
        raise RuntimeError('Storage Unit already exists: %s' % filename)

    try:
        filename.parent.mkdir(parents=True)
    except OSError:
        pass

    _LOG.info('Creating storage unit: %s', filename)

    nco = netcdf_writer.create_netcdf(str(filename), **(netcdfparams or {}))

    for name, coord in coordinates.items():
        netcdf_writer.create_coordinate(nco, name, coord.values, coord.units)

    netcdf_writer.create_grid_mapping_variable(nco, crs)

    for name, variable in variables.items():
        set_crs = all(dim in variable.dims for dim in crs.dimensions)
        var_params = variable_params.get(name, {})
        data_var = netcdf_writer.create_variable(nco, name, variable, set_crs=set_crs, **var_params)

        for key, value in var_params.get('attrs', {}).items():
            setattr(data_var, key, value)

    for key, value in (global_attributes or {}).items():
        setattr(nco, key, value)

    return nco


def write_dataset_to_netcdf(dataset, filename, global_attributes=None, variable_params=None,
                            netcdfparams=None):
    """
    Write a Data Cube style xarray Dataset to a NetCDF file

    Requires a spatial Dataset, with attached coordinates and global crs attribute.

    :param `xarray.Dataset` dataset:
    :param filename: Output filename
    :param global_attributes: Global file attributes. dict of attr_name: attr_value
    :param variable_params: dict of variable_name: {param_name: param_value, [...]}
                            Allows setting storage and compression options per variable.
                            See the `netCDF4.Dataset.createVariable` for available
                            parameters.
    :param netcdfparams: Optional params affecting netCDF file creation
    """
    global_attributes = global_attributes or {}
    variable_params = variable_params or {}
    filename = Path(filename)

    if not dataset.data_vars.keys():
        raise DatacubeException('Cannot save empty dataset to disk.')

    if not hasattr(dataset, 'crs'):
        raise DatacubeException('Dataset does not contain CRS, cannot write to NetCDF file.')

    nco = create_netcdf_storage_unit(filename,
                                     dataset.crs,
                                     dataset.coords,
                                     dataset.data_vars,
                                     variable_params,
                                     global_attributes,
                                     netcdfparams)

    for name, variable in dataset.data_vars.items():
        nco[name][:] = netcdf_writer.netcdfy_data(variable.values)

    nco.close()