"""
Module to run
:func:`~mirar.processors.astromatic.sextractor.sourceextractor.run_sextractor_single
 as a processor.
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

from astropy.io import fits
from astropy.table import Table

from mirar.data import Image, ImageBatch
from mirar.data.utils.coords import write_regions_file
from mirar.paths import (
    BASE_NAME_KEY,
    LATEST_WEIGHT_SAVE_KEY,
    get_output_dir,
    get_temp_path,
)
from mirar.processors.astromatic.sextractor.sourceextractor import (
    parse_checkimage,
    run_sextractor_single,
)
from mirar.processors.base_processor import BaseImageProcessor
from mirar.utils.ldac_tools import convert_table_to_ldac, get_table_from_ldac

logger = logging.getLogger(__name__)

SEXTRACTOR_HEADER_KEY = "SRCCAT"

sextractor_checkimg_map = {
    "BACKGROUND": "BKGPT",
    "BACKGROUND_RMS": "BKGRMS",
    "MINIBACKGROUND": "MINIBKG",
    "MINIBACK_RMS": "MINIBGRM",
    "SEGMENTATION": "SEGMAP",
    "-BACKGROUND": "BKGSUB",
}


class Sextractor(BaseImageProcessor):
    """
    Processor to run sextractor on images
    """

    base_key = "sextractor"
    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        output_sub_dir: str,
        config_path: str,
        parameter_path: str,
        filter_path: str,
        starnnw_path: str,
        saturation: float = None,
        verbose_type: str = "QUIET",
        checkimage_name: Optional[str | list] = None,
        checkimage_type: Optional[str | list] = None,
        gain: Optional[float] = None,
        cache: bool = False,
        mag_zp: Optional[float] = None,
        write_regions_bool: bool = False,
        catalog_purifier: Callable[[Table, Image], Table] = None,
    ):
        """
        :param output_sub_dir: subdirectory to output sextractor files
        :param config_path: path to sextractor config file
        :param parameter_path: path to sextractor parameter file
        :param filter_path: path to sextractor filter file
        :param starnnw_path: path to sextractor starnnw file
        :param saturation: saturation level for sextractor. Leave to None if not known,
        no saturation will be applied
        :param verbose_type: verbose type for sextractor
        :param checkimage_type: type of checkimage to output
        :param checkimage_name: name of checkimage to output. Leave to None to use
        pipeline defaults in sextractor_checkimage_map for output name (recommended).
        :param gain: gain for sextractor. Leave to None if not known.
        :param cache: whether to cache sextractor output
        :param mag_zp: magnitude zero point for sextractor. Leave to None if not known.
        :param write_regions_bool: whether to write regions file for ds9
        :param catalog_purifier: If not None, will apply this function to the
        Sextractor catalog before saving
        """
        # pylint: disable=too-many-arguments
        super().__init__()
        self.output_sub_dir = output_sub_dir
        self.config = config_path

        self.parameters_name = parameter_path
        self.filter_name = filter_path
        self.starnnw_name = starnnw_path
        self.saturation = saturation
        self.verbose_type = verbose_type
        self.checkimage_name = checkimage_name
        self.checkimage_type = checkimage_type
        self.gain = gain
        self.cache = cache
        self.mag_zp = mag_zp
        self.write_regions = write_regions_bool
        self.catalog_purifier = catalog_purifier

        if isinstance(self.checkimage_name, str):
            self.checkimage_name = [self.checkimage_name]
        if isinstance(self.checkimage_type, str):
            self.checkimage_type = [self.checkimage_type]

    def __str__(self) -> str:
        return (
            f"Processor to apply sextractor to images, "
            f"and save detected sources to the '{self.output_sub_dir}' directory."
        )

    def get_sextractor_output_dir(self) -> Path:
        """
        Get the directory to output

        :return: output directory
        """
        return get_output_dir(self.output_sub_dir, self.night_sub_dir)

    def _apply_to_images(self, batch: ImageBatch) -> ImageBatch:
        sextractor_out_dir = self.get_sextractor_output_dir()

        try:
            os.makedirs(sextractor_out_dir)
        except OSError:
            pass

        for image in batch:
            if self.gain is None and "GAIN" in image.keys():
                self.gain = image["GAIN"]

            temp_path = get_temp_path(sextractor_out_dir, image[BASE_NAME_KEY])

            if not os.path.exists(temp_path):
                self.save_fits(image, temp_path)

            temp_files = [temp_path]

            weight_path = None

            if LATEST_WEIGHT_SAVE_KEY in image.keys():
                image_weight_path = os.path.join(
                    sextractor_out_dir, image[LATEST_WEIGHT_SAVE_KEY]
                )
                temp_weight_path = get_temp_path(
                    sextractor_out_dir, image[LATEST_WEIGHT_SAVE_KEY]
                )
                if os.path.exists(image_weight_path):
                    shutil.copyfile(image_weight_path, temp_weight_path)
                    weight_path = temp_weight_path
                    temp_files.append(Path(weight_path))

            if weight_path is None:
                weight_path = self.save_mask_image(image, temp_path)
                temp_files.append(Path(weight_path))

            output_cat = sextractor_out_dir.joinpath(
                image[BASE_NAME_KEY].replace(".fits", ".cat")
            )

            _, checkimage_name = parse_checkimage(
                checkimage_name=None,
                checkimage_type=self.checkimage_type,
                image=os.path.join(sextractor_out_dir, image[BASE_NAME_KEY]),
            )

            logger.debug(f"Sextractor checkimage name is {checkimage_name}")

            output_cat, checkimage_name = run_sextractor_single(
                img=temp_path,
                config=self.config,
                output_dir=sextractor_out_dir,
                parameters_name=self.parameters_name,
                filter_name=self.filter_name,
                starnnw_name=self.starnnw_name,
                saturation=self.saturation,
                weight_image=weight_path,
                verbose_type=self.verbose_type,
                checkimage_name=checkimage_name,
                checkimage_type=self.checkimage_type,
                gain=self.gain,
                catalog_name=output_cat,
            )

            logger.debug(f"Cache save is {self.cache}")
            if not self.cache:
                for temp_file in temp_files:
                    os.remove(temp_file)
                    logger.debug(f"Deleted temporary file {temp_file}")

            if self.catalog_purifier is not None:
                output_catalog = get_table_from_ldac(output_cat)
                clean_catalog = self.catalog_purifier(output_catalog, image)
                clean_hdu = convert_table_to_ldac(clean_catalog)
                with fits.open(output_cat, memmap=False) as hdul:
                    clean_hdulist = fits.HDUList([hdul[0], hdul[1], clean_hdu[2]])
                    clean_hdulist.writeto(output_cat, overwrite=True)

            if self.write_regions:
                output_catalog = get_table_from_ldac(output_cat)

                x_coords = output_catalog["X_IMAGE"]
                y_coords = output_catalog["Y_IMAGE"]

                regions_path = output_cat.with_suffix(".reg")

                write_regions_file(
                    regions_path=regions_path,
                    x_coords=x_coords,
                    y_coords=y_coords,
                    system="image",
                    region_radius=5,
                )

            image[SEXTRACTOR_HEADER_KEY] = sextractor_out_dir.joinpath(
                output_cat
            ).as_posix()

            if len(checkimage_name) > 0:
                for i, checkimg_type in enumerate(self.checkimage_type):
                    image[sextractor_checkimg_map[checkimg_type]] = checkimage_name[i]

        return batch
