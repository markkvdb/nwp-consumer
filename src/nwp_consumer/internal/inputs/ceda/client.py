"""Client adapting CEDA API to internal Fetcher port."""

import datetime as dt
import pathlib
import typing
import urllib.parse
import urllib.request

import cfgrib
import numpy as np
import requests
import structlog
import xarray as xr

from nwp_consumer import internal

from ._models import CEDAFileInfo, CEDAResponse

log = structlog.getLogger()

# Defines parameters in CEDA that are not available from MetOffice
PARAMETER_IGNORE_LIST: typing.Sequence[str] = (
    "unknown",
    "h",
    "hcct",
    "cdcb",
    "dpt",
    "prmsl",
    "cbh",
)

COORDINATE_ALLOW_LIST: typing.Sequence[str] = ("time", "step", "x", "y")

# Defines the mapping from CEDA parameter names to OCF parameter names



class Client(internal.FetcherInterface):
    """Implements a client to fetch data from CEDA."""

    # CEDA FTP Username
    __username: str
    # CEDA FTP Password
    __password: str
    # FTP url for CEDA data
    __ftpBase: str

    def __init__(self, ftpUsername: str, ftpPassword: str) -> None:
        """Create a new CEDAClient.

        Exposes a client for CEDA's FTP server that conforms to the FetcherInterface.

        Args:
            ftpUsername: The username to use to connect to the CEDA FTP server.
            ftpPassword: The password to use to connect to the CEDA FTP server.
        """
        self.__username: str = urllib.parse.quote(ftpUsername)
        self.__password: str = urllib.parse.quote(ftpPassword)
        self.__ftpBase: str = f"ftp://{self.__username}:{self.__password}@ftp.ceda.ac.uk"

    def datasetName(self) -> str:
        """Overrides corresponding parent method."""
        return "UKV"

    def getInitHours(self) -> list[int]:
        """Overrides corresponding parent method."""
        return [0, 3, 6, 9, 12, 15, 18, 21]

    def listRawFilesForInitTime(self, *, it: dt.datetime) -> list[internal.FileInfoModel]:
        """Overrides corresponding parent method."""
        # Ignore inittimes that don't correspond to valid hours
        if it.hour not in self.getInitHours():
            return []

        # Fetch info for all files available on the input date
        # * CEDA has a HTTPS JSON API for this purpose
        response: requests.Response = requests.request(
            method="GET",
            url=f"https://data.ceda.ac.uk/badc/ukmo-nwp/data/ukv-grib/{it:%Y/%m/%d}?json",
        )

        if response.status_code == 404:
            # No data available for this init time. Fail soft
            log.warn(
                event="no data available for init time",
                init_time=f"{it:%Y/%m/%d %H:%M}",
                url=response.url,
            )
            return []
        if not response.ok:
            # Something else has gone wrong. Fail hard
            log.warn(
                event="error response from filelist endpoint",
                url=response.url,
                response=response.json(),
            )
            return []

        # Map the response to a CEDAResponse object to ensure it looks as expected
        try:
            responseObj: CEDAResponse = CEDAResponse.Schema().load(response.json())
        except Exception as e:
            log.warn(
                event="response from ceda does not match expected schema",
                error=e,
                response=response.json(),
            )
            return []

        # Filter the files for the desired init time
        wantedFiles: list[CEDAFileInfo] = [
            fileInfo for fileInfo in responseObj.items if _isWantedFile(fi=fileInfo, dit=it)
        ]

        return wantedFiles

    def downloadToCache(
        self, *, fi: internal.FileInfoModel,
    ) -> pathlib.Path:
        """Overrides corresponding parent method."""
        if self.__password == "" or self.__username == "":
            log.error(event="all ceda credentials not provided")
            return pathlib.Path()

        log.debug(event="requesting download of file", file=fi.filename(), path=fi.filepath())
        url: str = f"{self.__ftpBase}/{fi.filepath()}"
        try:
            response = urllib.request.urlopen(url=url)
        except Exception as e:
            log.warn(
                event="error calling url for file",
                url=fi.filepath(),
                filename=fi.filename(),
                error=e,
            )
            return pathlib.Path()

        # Stream the filedata into a cached file
        cfp: pathlib.Path = internal.rawCachePath(it=fi.it(), filename=fi.filename())
        with cfp.open("wb") as f:
            for chunk in iter(lambda: response.read(16 * 1024), b""):
                f.write(chunk)
                f.flush()

        log.debug(
            event="fetched all data from file",
            filename=fi.filename(),
            url=fi.filepath(),
            filepath=cfp.as_posix(),
            nbytes=cfp.stat().st_size,
        )

        return cfp

    def mapCachedRaw(self, *, p: pathlib.Path) -> xr.Dataset:
        """Overrides corresponding parent method."""
        if p.suffix != ".grib":
            log.warn(event="cannot map non-grib file to dataset", filepath=p.as_posix())
            return xr.Dataset()

        log.debug(event="mapping raw file to xarray dataset", filepath=p.as_posix())

        # Check the file has the right name
        if not any(setname in p.name.lower() for setname in ["wholesale1.grib", "wholesale2.grib"]):
            log.debug(
                event="skipping file as it does not match expected name",
                filepath=p.as_posix(),
            )
            return xr.Dataset()

        # Load the wholesale file as a list of datasets
        # * cfgrib loads multiple hypercubes for a single multi-parameter grib file
        # * Can also set backend_kwargs={"indexpath": ""}, to avoid the index file
        try:
            datasets: list[xr.Dataset] = cfgrib.open_datasets(
                path=p.as_posix(),
                chunks={"time": 1, "step": -1, "variable": -1, "x": "auto", "y": "auto"},
            )
        except Exception as e:
            log.warn(event="error converting raw file to dataset", filepath=p.as_posix(), error=e)
            return xr.Dataset()

        for i, ds in enumerate(datasets):
            # Ensure the temperature is defined at 1 meter above ground level
            # * In the early NWPs (definitely in the 2016-03-22 NWPs):
            #   - `heightAboveGround` only has one entry ("1" meter above ground)
            #   - `heightAboveGround` isn't set as a dimension for `t`.
            # * In later NWPs, 'heightAboveGround' has 2 values (0, 1) and is a dimension for `t`.
            if "t" in ds and "heightAboveGround" in ds["t"].dims:
                ds = ds.sel(heightAboveGround=1)

            # Snow depth is in `m` from CEDA, but OCF expects `kg m-2`.
            # * A scaling factor of 1000 converts between the two.
            # * See "Snow Depth" entry in https://gridded-data-ui.cda.api.metoffice.gov.uk/glossary
            if "sde" in ds:
                ds = ds.assign(sde=ds["sde"] * 1000)

            # Delete unnecessary data variables
            for var_name in PARAMETER_IGNORE_LIST:
                if var_name in ds:
                    del ds[var_name]

            # Delete unwanted coordinates
            ds = ds.drop_vars(
                names=[c for c in ds.coords if c not in COORDINATE_ALLOW_LIST],
                errors="ignore",
            )

            # Put the modified dataset back in the list
            datasets[i] = ds

        # Merge the datasets back into one
        wholesaleDataset = xr.merge(
            objects=datasets,
            compat="override",
            combine_attrs="drop_conflicts",
        )

        del datasets

        # Add in x and y coordinates
        try:
            wholesaleDataset = _reshapeTo2DGrid(ds=wholesaleDataset)
        except Exception as e:
            log.warn(event="error reshaping to 2D grid", filepath=p.as_posix(), error=e)
            return xr.Dataset()

        # Map the data to the internal dataset representation
        # * Transpose the Dataset so that the dimensions are correctly ordered
        # * Rechunk the data to a more optimal size
        wholesaleDataset = (
            wholesaleDataset.rename({"time": "init_time"})
            .expand_dims("init_time")
            .transpose("init_time", "step", "y", "x")
            .sortby("step")
            .chunk(
                {
                    "init_time": 1,
                    "step": -1,
                    "y": len(wholesaleDataset.y) // 2,
                    "x": len(wholesaleDataset.x) // 2,
                },
            )
        )

        return wholesaleDataset

    def parameterConformMap(self) -> dict[str, internal.OCFParameter]:
        """Overrides corresponding parent method."""
        return {
            "10wdir": internal.OCFParameter.WindDirectionFromWhichBlowingSurfaceAdjustedAGL,
            "10si": internal.OCFParameter.WindSpeedSurfaceAdjustedAGL,
            "prate": internal.OCFParameter.RainPrecipitationRate,
            "r": internal.OCFParameter.RelativeHumidityAGL,
            "t": internal.OCFParameter.TemperatureAGL,
            "vis": internal.OCFParameter.VisibilityAGL,
            "dswrf": internal.OCFParameter.DownwardShortWaveRadiationFlux,
            "dlwrf": internal.OCFParameter.DownwardLongWaveRadiationFlux,
            "hcc": internal.OCFParameter.HighCloudCover,
            "mcc": internal.OCFParameter.MediumCloudCover,
            "lcc": internal.OCFParameter.LowCloudCover,
            "sde": internal.OCFParameter.SnowDepthWaterEquivalent,
        }


def _isWantedFile(*, fi: CEDAFileInfo, dit: dt.datetime) -> bool:
    """Check if the input FileInfo corresponds to a wanted GRIB file.

    :param fi: The File Info object describing the file to check
    :param dit: The desired init time
    """
    if fi.it().date() != dit.date() or fi.it().time() != dit.time():
        return False
    # False if item doesn't correspond to Wholesale1 or Wholesale2 files
    if not any(setname in fi.filename() for setname in ["Wholesale1.grib", "Wholesale2.grib"]):
        return False

    return True


def _reshapeTo2DGrid(*, ds: xr.Dataset) -> xr.Dataset:
    """Convert 1D into 2D array.

    In the grib files, the pixel values are in a flat 1D array (indexed by the `values` dimension).
    The ordering of the pixels in the grib are left to right, bottom to top.

    This function replaces the `values` dimension with an `x` and `y` dimension,
    and, for each step, reshapes the images to be 2D.

    :param ds: The dataset to reshape
    """
    # Adapted from https://stackoverflow.com/a/62667154 and
    # https://github.com/SciTools/iris-grib/issues/140#issuecomment-1398634288

    # Define geographical domain for UKV. Taken from page 4 of https://zenodo.org/record/7357056
    dx = dy = 2000
    maxY = 1223000
    minY = -185000
    minX = -239000
    maxX = 857000
    # * Note that the UKV NWPs y is top-to-bottom, hence step is negative.
    northing = np.arange(start=maxY, stop=minY, step=-dy, dtype=np.int32)
    easting = np.arange(start=minX, stop=maxX, step=dx, dtype=np.int32)

    if ds.sizes["values"] != len(northing) * len(easting):
        raise ValueError(
            f"dataset has {ds.sizes['values']} values, "
            f"but expected {len(northing) * len(easting)}",
        )

    # Create new coordinates,
    # which give the `x` and `y` position for each position in the `values` dimension:
    ds = ds.assign_coords(
        {
            "x": ("values", np.tile(easting, reps=len(northing))),
            "y": ("values", np.repeat(northing, repeats=len(easting))),
        },
    )

    # Now set `values` to be a MultiIndex, indexed by `y` and `x`:
    ds = ds.set_index(values=("y", "x"))

    # Now unstack. This gets rid of the `values` dimension and indexes
    # the data variables using `y` and `x`.
    return ds.unstack("values")
