"""
Models for the 'nights' table
"""
from datetime import date
from typing import ClassVar

from sqlalchemy import DATE, Column, Integer
from sqlalchemy.orm import Mapped, relationship

from mirar.database.base_model import BaseDB, date_field
from mirar.pipelines.winter.models.base_model import WinterBase


class NightsTable(WinterBase):  # pylint: disable=too-few-public-methods
    """
    Nights table in database
    """

    __tablename__ = "nights"
    __table_args__ = {"extend_existing": True}

    nid = Column(Integer, primary_key=True)  # Sequence starting at 1
    nightdate = Column(DATE, unique=True)
    exposures: Mapped["ExposuresTable"] = relationship(back_populates="night")


class Night(BaseDB):
    """
    A pydantic model for a night database entry
    """

    sql_model: ClassVar = NightsTable
    nightdate: date = date_field

    def exists(self) -> bool:
        """
        Checks if the pydantic-ified nightid exists the corresponding sql database

        :return: bool
        """
        return self._exists(values=self.nightdate, keys="nightdate")

    #
    # def increment_raw(self):
    #     self.rawcount += 1
    #     self.update_entry()
