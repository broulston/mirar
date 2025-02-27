"""
Module with generators for WINTER pipeline
"""
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
from astropy.io import fits
from astropy.table import Table

from mirar.catalog import Gaia2Mass
from mirar.catalog.base_catalog import CatalogFromFile
from mirar.catalog.vizier import PS1
from mirar.data import Image, ImageBatch
from mirar.data.utils.compress import decode_img
from mirar.database.constraints import DBQueryConstraints
from mirar.database.transactions import select_from_table
from mirar.errors.exceptions import ProcessorError
from mirar.paths import (
    MAGLIM_KEY,
    REF_CAT_PATH_KEY,
    SATURATE_KEY,
    SCI_IMG_KEY,
    ZP_KEY,
    ZP_STD_KEY,
    get_output_dir,
)
from mirar.pipelines.winter.config import (
    psfex_path,
    sextractor_reference_config,
    swarp_config_path,
)
from mirar.pipelines.winter.constants import winter_filters_map
from mirar.pipelines.winter.fourier_bkg_model import subtract_fourier_background_model
from mirar.pipelines.winter.models import RefComponent, RefQuery, RefStack
from mirar.pipelines.wirc.wirc_files import sextractor_astrometry_config
from mirar.processors.astromatic import PSFex
from mirar.processors.astromatic.sextractor.sextractor import Sextractor
from mirar.processors.astromatic.swarp.swarp import Swarp
from mirar.processors.base_catalog_xmatch_processor import (
    default_image_sextractor_catalog_purifier,
)
from mirar.processors.photcal import PhotCalibrator
from mirar.processors.split import SUB_ID_KEY
from mirar.references.local import RefFromPath
from mirar.references.wfcam.wfcam_query import UKIRTOnlineQuery
from mirar.references.wfcam.wfcam_stack import WFCAMStackedRef

logger = logging.getLogger(__name__)


class ReductionQualityError(ProcessorError):
    """Error raised when the quality of the reduction is too poor"""


# Swarp generators
def winter_reference_image_resampler_for_zogy(**kwargs) -> Swarp:
    """
    Generates a resampler for reference images

    :param kwargs: kwargs
    :return: Swarp processor
    """
    return Swarp(
        swarp_config_path=swarp_config_path, cache=False, subtract_bkg=False, **kwargs
    )


def winter_wfau_component_image_stacker(**kwargs) -> Swarp:
    """
    Generates a resampler for reference images

    :param kwargs: kwargs
    :return: Swarp processor
    """
    return Swarp(
        swarp_config_path=swarp_config_path,
        cache=False,
        include_scamp=False,
        combine=True,
        calculate_dims_in_swarp=True,
        subtract_bkg=True,
        center_type="ALL",
        **kwargs,
    )


def winter_reference_sextractor(output_sub_dir: str, gain: float) -> Sextractor:
    """Returns a Sextractor processor for WINTER reference images"""
    return Sextractor(
        **sextractor_reference_config,
        gain=gain,
        output_sub_dir=output_sub_dir,
        cache=False,
    )


def winter_reference_psfex(output_sub_dir: str, norm_fits: bool) -> PSFex:
    """Returns a PSFEx processor for WINTER"""
    return PSFex(
        config_path=psfex_path,
        output_sub_dir=output_sub_dir,
        norm_fits=norm_fits,
    )


def winter_astrostat_catalog_purifier(catalog: Table, image: Image) -> Table:
    """
    Default function to purify the photometric image catalog
    """

    return default_image_sextractor_catalog_purifier(
        catalog, image, edge_width_pixels=0, fwhm_threshold_arcsec=20.0
    )


def winter_photometric_catalog_generator(
    image: Image,
) -> Gaia2Mass | PS1 | CatalogFromFile:
    """
    Function to crossmatch WIRC to GAIA/2mass for photometry

    :param image: Image
    :return: catalogue
    """
    if REF_CAT_PATH_KEY in image.header:
        ref_cat_path = Path(image[REF_CAT_PATH_KEY])
        if ref_cat_path.exists():
            logger.debug(f"Loading reference catalog from {ref_cat_path}")
            return CatalogFromFile(catalog_path=ref_cat_path)

    filter_name = image["FILTER"]
    search_radius_arcmin = (
        np.max([image["NAXIS1"], image["NAXIS2"]])
        * np.max([np.abs(image["CD1_1"]), np.abs(image["CD1_2"])])
        * 60
    ) / 2.0

    if filter_name in ["J", "H"]:
        return Gaia2Mass(
            min_mag=10,
            max_mag=20,
            search_radius_arcmin=search_radius_arcmin,
            filter_name=filter_name,
            snr_threshold=20,
            cache_catalog_locally=True,
        )

    if filter_name in ["Y"]:
        return PS1(
            min_mag=10,
            max_mag=20,
            search_radius_arcmin=search_radius_arcmin,
            filter_name=filter_name.lower(),
            cache_catalog_locally=True,
        )

    err = f"Filter {filter_name} not recognised"
    logger.error(err)
    raise ValueError(err)


def winter_ref_photometric_img_catalog_purifier(catalog: Table, image: Image) -> Table:
    """
    Default function to purify the photometric image catalog
    """

    return default_image_sextractor_catalog_purifier(
        catalog, image, edge_width_pixels=100, fwhm_threshold_arcsec=4.0
    )


def winter_reference_phot_calibrator(_: Image, **kwargs) -> PhotCalibrator:
    """
    Generates a resampler for reference images

    :param _: image
    :param kwargs: kwargs
    :return: Swarp processor
    """

    return PhotCalibrator(
        ref_catalog_generator=winter_photometric_catalog_generator,
        write_regions=True,
        image_photometric_catalog_purifier=winter_ref_photometric_img_catalog_purifier,
        **kwargs,
    )


def ref_sextractor(image: Image):
    """
    Generates a sextractor instance for reference images to get photometry
    Args:
        image:

    Returns:

    """
    logger.debug(image)
    return Sextractor(
        output_sub_dir="phot",
        **sextractor_astrometry_config,
        write_regions_bool=True,
        cache=False,
    )


def winter_astrometric_ref_catalog_generator(image) -> Gaia2Mass | CatalogFromFile:
    """
    Function to generate a reference catalog for WINTER astrometry

    :return: catalogue
    """
    if REF_CAT_PATH_KEY in image.header:
        ref_cat_path = Path(image[REF_CAT_PATH_KEY])
        logger.debug(f"Looking for local reference catalog at {ref_cat_path}")
        if ref_cat_path.exists():
            logger.debug(f"Loading reference catalog from {ref_cat_path}")
            return CatalogFromFile(catalog_path=ref_cat_path)
    return Gaia2Mass(
        min_mag=7, max_mag=20, search_radius_arcmin=20, cache_catalog_locally=True
    )


def winter_ref_catalog_namer(image: Image, output_dir: Path) -> Path:
    """
    Function to name the reference catalog to use for WINTER astrometry
    """
    output_dir.mkdir(exist_ok=True, parents=True)
    ref_cat_path = (
        output_dir / f"field{image['FIELDID']}_{image['SUBDETID']}"
        f"_{image['FILTER']}.ldac.cat"
    )
    return ref_cat_path


def winter_astrometric_ref_catalog_namer(batch: ImageBatch) -> ImageBatch:
    """
    Function to name the reference catalog to use for WINTER astrometry
    """
    winter_reference_catalog_dir = get_output_dir(
        dir_root="astrometric", sub_dir="winter/reference_catalogs"
    )
    for ind, image in enumerate(batch):
        image[REF_CAT_PATH_KEY] = winter_ref_catalog_namer(
            image, winter_reference_catalog_dir
        ).as_posix()
        batch[ind] = image
    return batch


def winter_photometric_ref_catalog_namer(batch: ImageBatch) -> ImageBatch:
    """
    Function to name the reference catalog to use for WINTER astrometry
    """
    winter_reference_catalog_dir = get_output_dir(
        dir_root="photometric", sub_dir="winter/reference_catalogs"
    )
    winter_reference_catalog_dir.mkdir(exist_ok=True, parents=True)
    for ind, image in enumerate(batch):
        image[REF_CAT_PATH_KEY] = winter_ref_catalog_namer(
            image, winter_reference_catalog_dir
        ).as_posix()
        batch[ind] = image
    return batch


def winter_astrometry_sextractor_catalog_purifier(catalog: Table, _) -> Table:
    """
    Function to purify the Sextractor catalog for WINTER astrometry
    """
    clean_catalog = catalog[
        (catalog["FLAGS"] == 0) & (catalog["FWHM_IMAGE"] > 0) & (catalog["SNR_WIN"] > 0)
    ]
    return clean_catalog


def winter_stackid_annotator(batch: ImageBatch) -> ImageBatch:
    """
    Generates a stack id for WINTER images as the minimum of the RAWID of the
    images for which the stack was requested.

    :param batch: ImageBatch
    :return: ImageBatch with stackid added to the header
    """
    first_rawid = np.min([int(image["RAWID"]) for image in batch])
    for image in batch:
        image["STACKID"] = int(first_rawid)
    return batch


def winter_candidate_annotator_filterer(src_df: pd.DataFrame):
    """
    Function to perform basic filtering to weed out bad WIRC candidates with None
    magnitudes, to be added.
    :param src_df: Source dataframe
    :return: filtered dataframe
    """

    none_mask = (
        src_df["sigmapsf"].isnull()
        | src_df["magpsf"].isnull()
        | src_df["magap"].isnull()
        | src_df["sigmagap"].isnull()
    )

    mask = none_mask.values

    # Needing to do this because the dataframe is big-endian
    mask_inds = np.where(~mask)[0]
    filtered_df = pd.DataFrame([src_df.loc[x] for x in mask_inds]).reset_index(
        drop=True
    )

    if len(filtered_df) == 0:
        filtered_df = pd.DataFrame(columns=src_df.columns)

    # Pipeline (db) specific keywords
    filtered_df.loc[:, "magzpsci"] = filtered_df.loc[:, ZP_KEY]
    filtered_df.loc[:, "magzpsciunc"] = filtered_df.loc[:, ZP_STD_KEY]
    filtered_df.loc[:, "diffmaglim"] = filtered_df.loc[:, MAGLIM_KEY]

    if len(filtered_df) > 0:
        sci_resamp_image_path = filtered_df.loc[0, SCI_IMG_KEY]
        filtered_df.loc[:, "field"] = fits.getval(sci_resamp_image_path, "FIELDID")
        filtered_df.loc[:, "programpi"] = fits.getval(sci_resamp_image_path, "PROGPI")
        filtered_df.loc[:, "programid"] = fits.getval(sci_resamp_image_path, "PROGID")
        filtered_df.loc[:, "fid"] = fits.getval(sci_resamp_image_path, "FID")
        filtered_df.loc[:, "deprecated"] = False

    return filtered_df


def winter_candidate_avro_fields_calculator(src_df: pd.DataFrame) -> pd.DataFrame:
    """
    Function to calculate the AVRO fields for WINTER
    """
    hist_dfs = [
        pd.DataFrame(src_df["prv_candidates"].loc[x]) for x in range(len(src_df))
    ]

    jdstarthists, jdendhists = [], []
    for ind, hist_df in enumerate(hist_dfs):
        if len(hist_df) == 0:
            jdstarthists.append(src_df.loc[ind]["jd"])
            jdendhists.append(src_df.loc[ind]["jd"])
        else:
            jdstarthists.append(hist_df["jd"].min())
            jdendhists.append(hist_df["jd"].max())
    src_df["jdstarthist"] = jdstarthists
    src_df["jdendhist"] = jdendhists
    src_df["ndethist"] = [len(x) for x in hist_dfs]
    src_df["magdiff"] = src_df["magpsf"] - src_df["magap"]
    src_df["magfromlim"] = src_df["diffmaglim"] - src_df["magpsf"]
    src_df["d_to_x"] = src_df["NAXIS1"] - src_df["xpos"]
    src_df["d_to_y"] = src_df["NAXIS2"] - src_df["ypos"]
    src_df["mindtoedge"] = src_df[["xpos", "ypos", "d_to_x", "d_to_y"]].min(axis=1)
    src_df["cutout_science"] = src_df["cutoutScience"]
    src_df["cutout_template"] = src_df["cutoutTemplate"]
    src_df["cutout_difference"] = src_df["cutoutDifference"]
    nnegs, nbads, sumrat = [], [], []
    for _, src in src_df.iterrows():
        diff_cutout_data = decode_img(src["cutout_difference"])
        # Get central 5x5 pixels
        nx, ny = diff_cutout_data.shape
        diff_stamp = diff_cutout_data[
            nx // 2 - 3 : nx // 2 + 2, ny // 2 - 3 : ny // 2 + 2
        ]
        nnegs.append(np.sum(diff_stamp < 0))
        nbads.append(np.sum(np.isnan(diff_stamp)))
        sumrat.append(np.sum(diff_stamp) / np.sum(np.abs(diff_stamp)))

    src_df["nneg"] = nnegs
    src_df["nbad"] = nbads
    src_df["sumrat"] = sumrat

    return src_df


def winter_reference_stack_annotator(stacked_image: Image, image: Image) -> Image:
    """
    Generates a stack id for WINTER reference images
    """
    stackid = (
        f"{str(image.header['FIELDID']).rjust(5, '0')}"
        f"{str(image.header[SUB_ID_KEY]).rjust(2, '0')}"
        f"{str(winter_filters_map[image.header['FILTER']])}"
    )
    stacked_image["STACKID"] = int(stackid)
    stacked_image["FIELDID"] = image.header["FIELDID"]
    stacked_image[SUB_ID_KEY] = image.header[SUB_ID_KEY]
    return stacked_image


def winter_reference_generator(image: Image):
    """
    Generates a reference image for the winter data
    Args:
        db_table: Database table to search for existing image
        image: Image

    Returns:

    """
    components_image_dir = get_output_dir(
        dir_root="components", sub_dir="winter/references"
    )
    components_image_dir.mkdir(parents=True, exist_ok=True)

    filtername = image["FILTER"]
    # TODO if in_ukirt and in_vista, different processing
    fieldid = int(image["FIELDID"])
    subdetid = int(image[SUB_ID_KEY])
    logger.debug(f"Fieldid: {fieldid}, subdetid: {subdetid}")

    constraints = DBQueryConstraints(
        columns=["fieldid", SUB_ID_KEY.lower()],
        accepted_values=[fieldid, subdetid],
    )

    db_results = select_from_table(
        db_constraints=constraints,
        sql_table=RefStack.sql_model,
        output_columns=["savepath"],
    )

    if len(db_results) > 0:
        savepath = db_results["savepath"].iloc[0]
        if os.path.exists(savepath):
            logger.debug(f"Found reference image in database: {savepath}")
            return RefFromPath(path=savepath, filter_name=filtername)

    ukirt_query = UKIRTOnlineQuery(
        num_query_points=9,
        filter_name=filtername,
        use_db_for_component_queries=True,
        components_db_table=RefComponent,
        query_db_table=RefQuery,
        skip_online_query=False,
        component_image_subdir="winter/references/components",
    )
    return WFCAMStackedRef(
        filter_name=filtername,
        wfcam_query=ukirt_query,
        image_resampler_generator=winter_wfau_component_image_stacker,
        write_stacked_image=True,
        write_stack_sub_dir="winter/references/ref_stacks",
        write_stack_to_db=True,
        stacks_db_table=RefStack,
        component_image_sub_dir="components",
        references_base_subdir_name="winter/references",
        stack_image_annotator=winter_reference_stack_annotator,
    )


winter_history_deprecated_constraint = DBQueryConstraints(
    columns="deprecated", accepted_values="False", comparison_types="="
)


def winter_fourier_filtered_image_generator(batch: ImageBatch) -> ImageBatch:
    """
    Generates a fourier filtered image for the winter data
    """
    new_batch = []
    for image in batch:
        # First, set the nans in the raw_data to the median value
        raw_data = image.get_data()
        replace_value = np.nanmedian(raw_data)  # 0.0

        mask = image.get_mask()  # 0 is masked, 1 is unmasked

        raw_data[~mask] = replace_value

        filtered_data, sky_model = subtract_fourier_background_model(raw_data)

        # mask the data back
        filtered_data[~mask] = np.nan

        image.set_data(filtered_data)

        # Update the header
        image.header["MEDCOUNT"] = np.nanmedian(filtered_data)
        image.header[SATURATE_KEY] -= np.nanmedian(sky_model)
        new_batch.append(image)
    new_batch = ImageBatch(new_batch)
    return new_batch
