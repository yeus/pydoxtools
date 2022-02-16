# -*- coding: utf-8 -*-
"""
Created on Mon Dec 16 12:07:52 2019

@author: Thomas Meschede
"""

# alternatives to camelot:
# https://github.com/tabulapdf/tabula for "stream-tables"
# 


import functools
import hashlib
import logging
import operator
import re
import tempfile
import typing
from pathlib import Path

import numpy as np
import pandas as pd
# TODO: evaluate tabula as an additional table-read mechanism
import pdfminer
import pdfminer.high_level
import pdfminer.pdfdocument
import pdfminer.psparser
import pikepdf
import pydantic
from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams
from pdfminer.layout import LTChar, LTTextLineVertical, LTCurve, LTFigure, LTTextLine
from pdfminer.layout import LTTextContainer
from pdfminer.pdfinterp import resolve1
from pdfminer.pdfparser import PDFParser
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import KernelDensity

from pydoxtools import document, list_utils
from pydoxtools import geometry_utils as gu
from pydoxtools.geometry_utils import box_cols, x0, x1, y1, pairwise_txtbox_dist
from pydoxtools.settings import settings

logger = logging.getLogger(__name__)

memory = settings.get_memory_cache()

try:
    from functools import cached_property
except ImportError:
    from pydoxtools.class_utils import cached_property

# make slicing a bit more natural with multipl coordinates
idx = pd.IndexSlice

"""
# methods to  convert pdf to html/text:
  
> pdftohtml PFR-PR05-HRVI-6HD-Flyer-V1.00-SV003.pdf test.html

# the next one preserves layout in textfile
> pdftotext -layout PFR-PR05-HRVI-6HD-Flyer-V1.00-SV003.pdf output.txt
"""


def _set_log_levels():
    """default loglevels of the libraries used here are very verbose...
    so we can optionally decrease the verbosity here"""
    logging.getLogger('pdfminer.pdfinterp').setLevel(logging.WARNING)
    logging.getLogger('pdfminer.pdfdocument').setLevel(logging.WARNING)
    logging.getLogger('pdfminer').setLevel(logging.WARNING)
    # logging.getLogger('camelot').setLevel(logging.WARNING) #not needed anymore...


_set_log_levels()


def repair_pdf(pdf_file_path: str) -> str:
    """
    repairs pdf and saves it using new filename.
    pikepdf needs to be installed in oder for this to work.

    TODO: do this "in-memory" because our algorithm
            can now work in memory-only with pdfminer.six
    """
    # create a temporary file in memory:
    # prefer a ramfs for speed
    newfilepath = "/run/user/1000/outtmp.pdf"
    if not Path(newfilepath).is_dir():
        # if it doesn#t exist (for example in a container) use normal tmp directory...
        newfilepath = "/tmp/outtmp.pdf"
    # open using pikepdf and save pdf again to mitigate a lot
    # of the problems with pdfminer.six
    with pikepdf.open(pdf_file_path) as pdf:
        # num_pages = len(pdf.pages)
        # del pdf.pages[-1]
        # pdf.save(str(pdf_file)[-3:]+"bk.pdf")
        pdf.save(newfilepath)

    return newfilepath


class PDFRepairError(Exception):
    pass


class TableExtractionError(Exception):
    pass


def repair_pdf_if_damaged(function):
    """
    repairs a pdf if certain exceptions are thrown due to faulty
    pdf files.

    TODO: better description of the following
    The function tobe-wrapped HAS to have "pdf_file" as first parameter
    """

    @functools.wraps(function)
    def wrapper(pdf_file, *args, **kwargs):
        try:
            return function(pdf_file, *args, **kwargs)
        except pdfminer.psparser.PSSyntaxError:
            logger.debug("repairing pdf file using pikepdf")
            f_repaired = repair_pdf(pdf_file)
            return function(f_repaired, *args, **kwargs)
        except pdfminer.pdfdocument.PDFEncryptionError as E:
            logger.info(f"{pdf_file} might be encrypted, trying if it has an empty password... and repairing")
            # logger.exception(f"could not open pdf document {pdf_file} it might be encrypted... "
            #                 f"(sometimes with an empty password)?:\n ({E})")
            f_repaired = repair_pdf(pdf_file)
            # TODO: catch encryption errior from the above command and return a string
            # that says "pdf is encrypted" or something similar...
            # or raise another exception that says: pdf is definitly encrypted1
            return function(f_repaired, *args, **kwargs)
            # logger.info("try to repair pdf:")
            # repair_pdf(repair_pdf_if_damaged())
        except IndexError:
            logger.exception("some sort of index error might be caused by this bug here:  "
                             "https://github.com/pdfminer/pdfminer.six/issues/218")
            logger.debug("trying to repair pdf file using pikepdf")
            f_repaired = repair_pdf(pdf_file)
            return function(f_repaired, *args, **kwargs)
        except:
            logger.exception(f"Not able to process pdf: {pdf_file}")
            raise PDFRepairError(f"Not able to process pdf: {pdf_file}")

    return wrapper


def _get_meta_infos(f):
    try:
        fp = open(f, 'rb')
        parser = PDFParser(fp)
    except TypeError:  # probably have a filepath and not a file-like-obj
        fp = None
        parser = PDFParser(f)

    doc = pdfminer.pdfdocument.PDFDocument(parser)

    try:
        pagenum = resolve1(doc.catalog['Pages'])['Count']
    except (AttributeError, TypeError):
        logger.warning(f"could not read pagenumber of {f}, trying the 'slow' method")
        pagenum = sum(1 for p in extract_pages(f))

    res = {
        **(doc.info[0]),
        "pagenum": pagenum
    }
    if fp:
        fp.close()
    return res


get_meta_infos_safe = repair_pdf_if_damaged(_get_meta_infos)
get_meta_infos_safe_cached = memory.cache(get_meta_infos_safe)


def _filter_boxes(
        boxes: pd.DataFrame,
        min_aspect_ratio=None,
        max_aspect_ratio=None,
        min_num=None,
        min_area=None,
):
    """filter boxes for various criteria

    TODO: optionally include more filter criteria...
        - min size
        - min margin
        - min size
        - max size
        - min_width
        - min_height
    """
    if not boxes.empty:
        if min_num:
            boxes = boxes[boxes["num"] >= min_num].copy()
        boxes['w'] = boxes.x1 - boxes.x0
        boxes['h'] = boxes.y1 - boxes.y0
        boxes['aspect_ratio'] = boxes.h / boxes.w
        boxes['area'] = boxes.w * boxes.h
        if min_area:
            boxes = boxes.loc[boxes.area > min_area]
        if min_aspect_ratio:
            boxes = boxes.loc[boxes.aspect_ratio > min_aspect_ratio]
        if max_aspect_ratio:
            boxes = boxes.loc[boxes.aspect_ratio < max_aspect_ratio]
    else:
        boxes = pd.DataFrame()

    return boxes


def _calculate_ge_points_density_metrics(points):
    """calculate vertical point density (edge density would be without vertical lines)

    a,b = _calculate_ge_points_density_metrics(points[["y0","y1"]].values.flatten())
    pd.DataFrame([a,b]).T.plot(x=0,y=1)
    """
    # get unique point with their count and store it in an array
    ps = np.array(np.unique(points, return_counts=True))
    # the following calculates a density function with a bandwidth of 0:
    # we replaced this with the KDE below to get better results.
    # pd = ps[1]/np.diff(y[0], append=[0])

    # because we use "tophat" function, badwidth can be interpreted as pdf-pixels
    # (standard pdf coordinate system at 72dpi)
    bw = 2.0
    kde = KernelDensity(bandwidth=bw, kernel="tophat", rtol=0.5, metric="manhattan").fit(ps[0][:, None],
                                                                                         sample_weight=ps[1])
    ge_density = np.exp(kde.score_samples(ps[0][:, None])) * bw
    # pd.DataFrame(y).T.plot(x=0,y=1)
    # pd.DataFrame([y[0],gaussian_kde(y[0], 0.1, y[1])(y[0])]).T.plot(x=0,y=1)
    return ps[0], ge_density


def _line2txt(LTOBJ: typing.Iterable):
    """
    extract text from pdfiner.six lineobj including size hints

    TODO: speedup using cython/nuitka/numba
    """
    txt = ""
    last_size = 0
    for i, ch in enumerate(LTOBJ):
        newtxt = ""
        sizehint = ""
        if isinstance(ch, pdfminer.layout.LTText):
            newtxt = ch.get_text()
        if isinstance(ch, pdfminer.layout.LTChar):
            newsize = ch.size
            if i > 0:
                # TODO: use an iterative function here...
                if newsize < last_size:
                    sizehint = "<s>"
                elif newsize > last_size:
                    sizehint = "</s>"
            last_size = newsize
        txt += sizehint + newtxt
    return txt


def _get_cell_text(cell, min_new_line_gap=4, strip=False):
    # TODO: make sure we preserve textblocks...
    cell["sortkey"] = cell.x0 - 40 * cell.y0
    sorted_cell = cell.sort_values(by="sortkey").reset_index(drop=True)
    # check for newlines
    if strip:
        sorted_cell.text = sorted_cell.text.str.strip()
    newline = sorted_cell.y0.diff() < -min_new_line_gap
    cell_text = "".join(("\n" if nl else " ") + t for t, nl in zip(sorted_cell.text, newline))[1:]
    return cell_text


def _get_cell_coordinates(cell_edges, table_lines, tol=5.0):
    c = cell_edges + [+tol, -tol]  # make cells "thinner" to more easily fit inside table_lines
    c0, c1 = c.T
    tc = np.argwhere((c0[:, None] < table_lines[1:]) & (c1[:, None] > table_lines[:-1]))
    tc = np.split(tc[:, 1], np.unique(tc[:, 0], return_index=True)[1][1:])
    return tc


def _generate_text_boxes(df: pd.DataFrame) -> pd.DataFrame:
    """
    create textboxes and create bounding boxes and aggregated text from
    a pandas dataframe with textlines.
    returns a list of textboxes together wth some coordinate data and
    the contained text.

    TODO: integrate this function with the classes above..

    TODO: make it possible to use alternative distance metrics to generate
          the text boxes...
    """
    if "boxnum" in df:
        group = df.groupby(['p_id', 'boxnum'])
        # aggregate object from the same box and calculate new
        # bounding boxes, also join the formatted text
        bg = group.agg(
            x0=("x0", "min"), y0=("y0", "min"),
            x1=("x1", "max"), y1=("y1", "max"),
            text=("lineobj",
                  lambda x: "".join(_line2txt(obj) for obj in x.values))
        )
        # remove empty box_groups
        bg = bg[bg.text.str.strip().str.len() > 1].copy()
        # do some calculations
        bg['y_mean'] = bg[['y0', 'y1']].mean(axis=1)
        bg['x_mean'] = bg[['x0', 'x1']].mean(axis=1)
        bg['w'] = bg.x1 - bg.x0
        bg['h'] = bg.y1 - bg.y0
        return bg
    else:
        return pd.DataFrame(columns=['text'])


def _LTObj2Chars(df_le) -> pd.DataFrame:
    chars = []
    for text_line in df_le.lineobj.values:
        for character in text_line:
            if isinstance(character, LTChar):
                chars.append(character)
    chars = pd.DataFrame([dict(
        obj=c,
        x0=c.bbox[0],
        y0=c.bbox[1],
        x1=c.bbox[2],
        y1=c.bbox[3],
    ) for c in chars])
    return chars


def _close_cell(oc, df_le, y1, text_line_tol):
    # the current indexing of text lines is the following:
    # "y0","x0","y0","x1" and so we need to
    # to do the indexing accordingly
    # TODO: not sure if "min" is the right operation here, it might also be enough
    #       to just use "iloc[0]" due to already correct sorting...
    oc['y1'] = y1
    text_elements = df_le.loc[idx[
                              oc["y0"] - text_line_tol:,
                              oc["x0"] - text_line_tol:,
                              :oc["y1"] + text_line_tol,
                              :oc["x1"] + text_line_tol],
                    :]
    if text_elements.empty:
        return None
    else:
        oc['text_elements'] = text_elements
        return oc


def _close_open_cells(open_cells, h_lines, df_le, elem_scan_tol,
                      text_line_tol, y0_cursor):
    # iterate through each open cell and check which ones we can close with the
    # provided top_cell_border_elements
    still_open = []
    new_cells = []
    for oc in open_cells:
        # check if we have a top element that somehow exists within the cell borders
        # and can be used to close the cell...
        # we have 3 cases:
        #  - both ends are inside the cell
        #  - x0 is on the left side of the left lineend
        #  - x1 is on the right side of the right line end
        # all three cases are handled by the expression below:
        top_elem = h_lines.loc[
                   idx[:oc['x1'] - elem_scan_tol,
                   oc['x0'] + elem_scan_tol:],
                   :]

        if top_elem.empty:
            still_open.append(oc)
        else:  # if top_elem exits, close the cell
            if new_cell := _close_cell(oc, df_le, y0_cursor, text_line_tol):
                new_cells.append(new_cell)

    return new_cells, still_open


class PDFBase(document.Base):
    """
    This class collects functions that are valid for pages, as well as entire documents...
    """

    @property
    def df_le(self) -> pd.DataFrame:
        """this method should provide a dataframe of the extracted line elements of a pdf"""
        return pd.DataFrame()

    @cached_property
    def txt_box_df(self) -> pd.DataFrame:
        if self.df_le.empty:
            return pd.DataFrame()
        boxes = _generate_text_boxes(self.df_le)
        return boxes

    @cached_property
    def textboxes(self) -> typing.List:
        boxes = self.txt_box_df
        return boxes.get("text", pd.Series()).to_list()

    @cached_property
    def full_text(self) -> str:
        return "\n".join(self.textboxes)

    @functools.lru_cache()
    def __detect_titles(self) -> pd.DataFrame:
        """
        detects titles and interesting textpieces from a list of text lines
        TODO: convert this function into a function of "feature-generation"
              and move the anomaly detection into the cached_property functions
        """

        # TODO: extract the necessary features that we need here "on-the-fly" from
        #       LTLineObj
        # extract more features for every line
        dfl = self.df_le.copy()
        dfl[['font', 'size', 'color']] = dfl.font_infos.apply(
            lambda x:
            pd.Series(max(x, key=operator.itemgetter(1)))
        )

        # generate some more features
        dfl['text'] = dfl.rawtext.str.strip()
        dfl = dfl.loc[dfl.text.str.len() > 0].copy()
        dfl['length'] = dfl.text.str.len()
        dfl['wordcount'] = dfl.text.str.split().apply(len)
        dfl['vertical'] = dfl.lineobj.apply(lambda x: isinstance(x, LTTextLineVertical))

        dfl = dfl.join(pd.get_dummies(dfl.font, prefix="font"))
        dfl = dfl.join(pd.get_dummies(dfl.font, prefix="color"))

        features = set(dfl.columns) - {'text', 'font_infos', 'font', 'rawtext', 'lineobj', 'color'}

        # detect outliers to isolate titles and other content from "normal"
        # content
        # TODO: this could be subject to some hyperparameter optimization...
        df = dfl[list(features)]
        clf = IsolationForest()  # contamination=0.05)
        clf.fit(df)
        dfl['outliers'] = clf.predict(df)

        return dfl

    @cached_property
    def titles(self) -> typing.List:
        if self.df_le.empty:
            return []
        dfl = self.__detect_titles()
        # titles = l.query("outliers==-1")
        titles = dfl.query("outliers==-1 and wordcount<10")
        titles = titles[titles['size'] >= titles['size'].quantile(0.75)]
        return titles.get("text", pd.Series()).to_list()

    @cached_property
    def side_titles(self) -> pd.DataFrame:
        if self.df_le.empty:
            return pd.DataFrame()
        dfl = self.__detect_titles()
        # TODO: what to do with side-titles?
        side_titles = dfl.query("outliers==-1 and wordcount<10")
        side_titles = side_titles[side_titles['size'] > dfl['size'].quantile(0.75)]
        # titles = titles[titles['size']>titles['size'].quantile(0.75)]
        return side_titles

    @cached_property
    def side_content(self) -> str:
        if self.df_le.empty:
            return ""
        # TODO: extract side-content such as addresses etc..
        dfl = self.__detect_titles()
        side_content = "\n---\n".join(dfl[dfl.outliers == -1].text)
        return side_content

    @cached_property
    def main_content(self) -> str:
        if self.df_le.empty:
            return ""
        dfl = self.__detect_titles()
        main_content = "\n---\n".join(dfl[dfl.outliers == 1].text)
        return main_content


class TableExtractionParameters(pydantic.BaseModel):
    # possible values: "t", "g", "tg", with "t" for text-based and "g" for graphics based
    extraction_method: str = "g"
    min_table_area: float = 50.0 * 50.0  # in dots * dots, using pdf coordinates (72dpi)
    # aspect ratios are calculated as h/w
    min_aspect_ratio: float = 1.0 / 50.0  # exclude for example single lines
    max_aspect_ratio: float = 50.0  # exclude for example high graphic elements...
    # parameters for the distance function
    # a list of dicts parameters for each iteration where each dict includes
    # every used sub-distance-function, where each key in the dictionary
    # resembles the function and the values the parameters for that specific function
    # TODO: document this in a better way...
    area_detection_distance_func_params: typing.List[typing.Dict[str, typing.List[float]]]
    # on top of the table area we put a margin which helps
    # when area detection was inaccurate
    text_extraction_margin: float = 10.0

    #  #table cell extraction
    max_v_line_thickness: float = 5.0  # maximum thickness which is allowed for a vertical line
    elem_scan_tol: float = 1.0  # used in several places where we want to know coordinates of lines

    # the x-cursor has to advance at least this far in order for anew cell to be created
    # otherwise it simply omits the vertical line element.
    min_cell_width: float = 6.0

    # tolerance which determines which text lines will be added to a cell area
    # this should stay below a typical line-height in pixels (>8 )in order to
    # avoid adding neighbouring textlines...
    text_line_tol: float = 5.0

    # table conversion to dataframe
    table_line_merge_tol: float = 7.0  # to which extend will neighbouring cell lines be merged into a table-column/row?
    cell_idx_tol: float = 5.0  # how precise should the coordinates be for each cell in relation to the table lines?

    # TODO: make these parameters relative
    # min_char_line_overlap the tolerance to which extend cahracters have to be in the same
    # line to be considered part of the same word
    max_char_disalignement: float = 4.0
    # maximum distance of characters to be considered in the same line...
    # this should be low for tables...
    max_char_dist: float = 2.0

    @classmethod
    def reduced_params(cls):
        hp = {'es1': 11.1, 'es2': 2.1, 'gs1': 11.1, 'gs2': 20.1}
        adp = [{
            "va": [hp['gs1'], hp['es1'], hp['es1'] / 2, hp['es1']],
            "ha": [hp['gs1'], hp['es1'], hp['es1'] / 2, hp['es1']]
        },
            {
                "va": [hp['gs2'], hp['es2'], hp['es2'] / 2, hp['es2']],
                "ha": [hp['gs2'], hp['es2'], hp['es2'] / 2, hp['es2']]
            }]
        return cls(
            area_detection_distance_func_params=adp,
        )


class PDFDocument(PDFBase):
    """
    Loads a pdf file and can extract all kinds of information from it.

    - we extract all textlines with some metadata such as color, textsize
    - use that information to extract "outliers" which are assumed to hold
      important information and represent for example titles of textboxes
    - extract assumed titles by text-size and word count
    - extract lines which have "list" characters such as "*" or "-"
    - join lines that are part of the same "box" as "textboxes"
    - extract table data

    TODO: move extract_elements into the page class...
    """

    def __init__(
            self,
            fobj: typing.Union[str, typing.BinaryIO],
            source,
            table_extraction_params: typing.Optional[TableExtractionParameters] = None,
            page_numbers=None, maxpages=0,
            laparams=LAParams()
    ):
        """

        :param fobj: a file-like object for the PDF file
                     to be worked on.

        :param laparams: An LAParams object from pdfminer.layout. If None, uses
        some default settings that often work well.

        LAParams(
           line_overlap=0.5,  # 0.5, are chars in the same line?
           char_margin=2.0,  # 2.0, max distance between chars in words
           word_margin=0.1,  # 0.1, max distance between words in line
           line_margin=0.5,  # 0.5, max distance between lines in box
           boxes_flow=+0.5,  # 0.5, box order
           detect_vertical=False,
           all_texts=False
        )
        """
        super().__init__(fobj, source)
        self.laparams = laparams
        self.page_numbers = page_numbers
        self.maxpages = maxpages
        if table_extraction_params:
            # override default parameters with those from function call:
            self.tbe = table_extraction_params
        else:
            # set default table extraction parameters
            self.tbe = TableExtractionParameters.reduced_params()

    @classmethod
    def pre_initialized(cls, fobj, **kwargs) -> "PDFDocument":
        return cls(fobj, **kwargs).pre_cache()

    @classmethod
    @memory.cache
    def from_disk_cache(cls, fobj, **kwargs) -> "PDFDocument":
        """return a pre-initialized document from disk_cache"""
        return repair_pdf_if_damaged(cls.pre_initialized)(fobj, **kwargs)
        # without repair:
        # return cls(fobj, table_extraction_params, page_numbers, maxpages).pre_cache()

    @classmethod
    @memory.cache
    def with_cached_elements(cls, fobj, **kwargs) -> "PDFDocument":
        def extract(f):
            new = cls(f, **kwargs)
            cache = new.df_le, new.df_ge
            return new

        return repair_pdf_if_damaged(extract)(fobj)

    def pre_cache(self):
        """in some situations, for example for caching purposes it would be nice
        to pre-cache all calculations this is done here by simply calling all functions..."""
        # TODO: for some reason we can only use table_metrics right now for caching...
        #       try other things as well? at least this already gives us quiet a bit of
        #       time savings...
        res = (self.table_metrics, self.titles)
        # res = (
        #    self.pages, self.tables, self.table_metrics, self.side_content,
        #    self.textboxes, self.full_text, self.list_lines, self.main_content,
        #    self.titles, self.meta_infos, self.side_titles, self.pages_bbox
        # )
        # also cache all pages...
        # for p in self.pages:
        #    p.pre_cache()

        return self

    @functools.lru_cache()
    def extract_pdf_elements(self) -> typing.Tuple[
        typing.List, typing.List, typing.Set, typing.Dict]:
        """
        extracts all text lines from a pdf and annotates them with various features.

        TODO: move this class into the "PDFPage" class in order to save resources...

        TODO: make use of other pdf-pobjects as well (images, figures, drawings  etc...)

        TODO: check for already extracted pages and only extract missing ones...

        TODO: implement our own algorithm in order to identify textboxes...  the pdfminer.six
              one has problems with boxes when there is a line with a right- and a left justified
              text in the same line..  in most cases they should be split into two boxes...
        """
        lines = []
        graphic_elements = []
        # TODO: automatically classify text pieces already at this point here for example
        #       to find addresses, hint to tables etc... the rest of the algorithm would get a lot
        #       more precise this way...
        extracted_page_numbers = set()
        pages_bbox = {}
        # TODO: perform this lazily for each page
        if isinstance(self.fobj, tempfile.SpooledTemporaryFile):
            fobj = self.fobj._file
        else:
            fobj = self.fobj
        for page_layout in extract_pages(fobj,
                                         laparams=self.laparams,
                                         page_numbers=self.page_numbers,
                                         maxpages=self.maxpages):
            extracted_page_numbers.add(page_layout.pageid)
            pages_bbox[page_layout.pageid] = page_layout.bbox
            for boxnum, element in enumerate(page_layout):
                if isinstance(element, LTCurve):  # LTCurve are rectangles AND lines
                    graphic_elements.append(dict(
                        gobj=element,
                        linewidth=element.linewidth,
                        non_stroking_color=element.non_stroking_color,
                        stroking_color=element.stroking_color,
                        stroke=element.stroke,
                        fill=element.fill,
                        evenodd=element.evenodd,
                        p_id=page_layout.pageid,
                        boxnum=boxnum,
                        x0=element.x0,
                        y0=element.y0,
                        x1=element.x1,
                        y1=element.y1
                    ))
                elif isinstance(element, LTTextContainer):
                    if isinstance(element, LTTextLine):
                        element = [element]
                    for linenum, text_line in enumerate(element):
                        fontset = set()
                        # TODO: this could be moved somehwere else and probably be made more efficient
                        for character in text_line:
                            if isinstance(character, LTChar):
                                charfont = (
                                    character.fontname, character.size,
                                    str(character.graphicstate.ncolor))
                                fontset.add(charfont)
                        linetext = text_line.get_text()
                        # extract metadata
                        # TODO: move most of these function to a "feature-generation-function"
                        # which extracts the information directly from the LTTextLine object
                        lines.append(dict(
                            lineobj=text_line,
                            rawtext=linetext,
                            font_infos=fontset,
                            p_id=page_layout.pageid,
                            linenum=linenum,
                            boxnum=boxnum,
                            x0=text_line.x0,
                            y0=text_line.y0,
                            x1=text_line.x1,
                            y1=text_line.y1
                        ))
                elif isinstance(element, LTFigure):
                    # TODO: use pdfminer.six to also group Char in LTFigure
                    # TODO: extract text from figures as well...
                    # chars =[e for e in list_utils.flatten(element, max_level=1)):
                    # list(list_utils.flatten(element, max_level=1))
                    # txt = "".join(e.get_text() for e in list_utils.flatten(element) if isinstance(e, LTChar))
                    # es = list(list_utils.flatten(element))
                    # import pdfminer.converter
                    pass

        # do some more calculations

        return lines, graphic_elements, extracted_page_numbers, pages_bbox

    @cached_property
    def df_le(self) -> pd.DataFrame:
        """line elements of page"""
        lines, _, _, _ = self.extract_pdf_elements()
        # TODO: make this more generic mayb by declaring a line object and using those properties?
        return pd.DataFrame(lines, columns=[
            "lineobj", "rawtext", "font_infos", "p_id",
            "linenum", "boxnum", "x0", "y0", "x1", "y1"])

    @cached_property
    def df_ge(self) -> pd.DataFrame:
        """line elements of page"""
        _, graphic_elements, _, _ = self.extract_pdf_elements()
        return pd.DataFrame(graphic_elements)

    @cached_property
    def extracted_pages(self) -> typing.Set:
        _, _, extracted_page_numbers, _ = self.extract_pdf_elements()
        return extracted_page_numbers

    @cached_property
    def pages_bbox(self) -> typing.Dict:
        _, _, _, pages_bbox = self.extract_pdf_elements()
        return pages_bbox

    @cached_property
    def list_lines(self) -> typing.List[str]:
        """
        Extract lines that might be part of a "list".

        TODO: make this page-based as well
        """
        # search for lines that are part of lists
        # play around with this expression here: https://regex101.com/r/xrnKlm/1
        degree_search = r"^[\-\*∙•](?![\d\-]+\s?(?:(?:[°˚][CKF]?)|[℃℉]))"
        has_list_char = self.df_le.rawtext.str.strip().str.contains(degree_search, regex=True, flags=re.UNICODE)
        list_lines = self.df_le[has_list_char].rawtext.str.strip().tolist()

        return list_lines

    @cached_property
    def pages(self) -> typing.List["PDFPage"]:
        # TODO: get number of pages from "meta_infos"
        return [PDFPage(p, self) for p in self.extracted_pages]

    @cached_property
    def table_objs(self):
        return [t for p in self.pages for t in p.tables]

    @cached_property
    def tables(self):
        tables = [df.to_dict('index') for df in self.tables_df]
        # append lists as a 1-D table as well...
        tables.append({i: {0: line} for i, line in enumerate(self.list_lines)})
        return tables

    @cached_property
    def tables_df(self):
        return [t.df for p in self.pages for t in p.tables]

    @cached_property
    def table_metrics(self):
        return [t.metrics for p in self.pages for t in p.tables]

    @cached_property
    def table_metrics_X(self) -> pd.DataFrame:
        return pd.DataFrame([t.metrics_X for p in self.pages for t in p.tables])

    @cached_property
    def meta_infos(self):
        # TODO: generalize the "safe" calling of functions for pdfs somehow...
        #       maybe by using a cached "safe" pdf file? or of something goes wrong, simply replacing the fobj
        #       with a repaired pdf?
        meta = [list_utils.deep_str_convert(get_meta_infos_safe(self.fobj))]
        return meta


class PDFPage(PDFBase):
    def __init__(self, pagenum: int, parent_doc: PDFDocument):
        self.pagenum = pagenum
        self.parent_document = parent_doc

    @property
    def tbe(self) -> TableExtractionParameters:
        return self.parent_document.tbe

    def pre_cache(self):
        """in some situations, for example for caching purposes it would be nice
        to pre-cache all calculations this is done here by simply calling all functions..."""
        res = (
            self.tables, self.side_content,
            self.textboxes, self.full_text, self.main_content,
            self.titles, self.side_titles
        )
        return self

    @cached_property
    def page_bbox(self):
        # pikepdf:  p.mediabox
        return self.parent_document.pages_bbox[self.pagenum]

    @cached_property
    def area(self) -> float:
        return self.page_bbox[2] * self.page_bbox[3]

    @cached_property
    def df_ge_f(self) -> pd.DataFrame:
        """get filtered graph elements by throwing out a lot of unnecessary graphics
        elements which are probably not part of a table

        # TODO: parameterize this functions for hyperparameter tuning...

        we want to filter out a lot of graphics element which are not relevant. Those include:
        - very small elements
        - elements far away from text
        - elements which are are not a box or a vertical/horizontal line

        check out of we a lot of elements with "similar" x- or y coordinates in
        order to filter out "non-ordered" elements which
        we assume to be non-table graphical elments

        TODO: some of the following operations might be more efficient in the table-graph-search
        """

        # TODO: filter specifically for each coordinate
        # TODO: filter for "unique-coordinate-density"
        #       as tables are ordered, we can assume that if we
        #       seach for "unique" coordinates, the density of
        #       tables should be even lower than that for figures.
        #       because of this we can theoretically improve the
        #       accuracy of this filter.

        # TODO: use these as hyperparameters
        min_size = 5.0  # minimum size of a graphics element
        margin = 20  # margin of the page
        max_area_page_ratio = 0.4  # maximum area on a page to occupy by a graphics element

        # get minimum length for lines by searching for
        # the minimum height/width of a text box
        # we do this, because we assume that graphical elements should be at least this
        # big in order to be part of a table
        min_elem_x = max(self.txt_box_df.w.min(), min_size)
        min_elem_y = max(self.txt_box_df.h.min(), min_size)

        # check if the following calculations are duplicated anywhere...
        ge = self.df_ge
        ge['w'] = ge.x1 - ge.x0
        ge['h'] = ge.y1 - ge.y0
        ge['area'] = ge.w * ge.h
        ge['area_ratio'] = (ge.area / self.area)
        # df_ge['w+h'].hist(bins=100)
        # filter out all elements that occupy more than max_area_page_ratio of page space
        ge_v = ge[ge.area_ratio < max_area_page_ratio]
        # filter out all elements that are thinner than min_elem_x- and y
        # under certain conditions
        ge_v = ge_v.query('((w>h) and (w>@min_elem_x)) or ((h>w) and (h>@min_elem_y))').copy()
        # elements that are "outside the page area/margin should be discarded
        # "standard" margin
        ge_v = ge_v[((ge_v[box_cols[2:]] + margin) < (self.page_bbox[2:])).all(1)
                    & ((ge_v[box_cols[:2]] - margin) > (self.page_bbox[:2])).all(1)]
        # use the smallest rectangle which encloses textboxes plus a small margin
        # min_x = self.box_groups.x0.min()
        # max_x = self.box_groups.x1.max()
        # min_y = self.box_groups.y0.min()
        # max_y = self.box_groups.y1.max()
        # ge_v = ge_v[((ge_v.x0 + margin) > min_x) & ((ge_v.x1 - margin) < max_x)
        #             & ((ge_v.y0 + margin) > min_y) & ((ge_v.y1 - margin) < max_y)]

        # elements

        return ge_v

    @cached_property
    def df_le(self) -> pd.DataFrame:
        """line elements of page"""
        if not self.parent_document.df_le.empty:
            return self.parent_document.df_le.loc[self.parent_document.df_le.p_id == self.pagenum].copy()
        else:
            return pd.DataFrame()  # empty dataframe

    @cached_property
    def df_ge(self) -> pd.DataFrame:
        """graphic elements of page"""
        if not self.parent_document.df_le.empty:
            return self.parent_document.df_ge[
                self.parent_document.df_ge.p_id == self.pagenum].copy()
        else:
            return pd.DataFrame()  # empty dataframe

    @property
    def tables(self) -> typing.List["PDFTable"]:
        if self.df_le.empty:
            return []
        return [t for t in self.table_candidates if t.is_valid]

    @property
    def table_areas(self) -> pd.DataFrame:
        return pd.DataFrame([t.bbox for t in self.tables])

    @property
    def distance_threshold(self) -> float:
        # we keep this number constant as the same effect can be gained
        # through tbe.area_detection_params but a lot more fine grained as
        # it directly controls the sensitivity of the distance function
        return 10.0  # merge everything with a distance of less than 10..

    def detect_table_area_candidates(self):
        """
        Detect tables from elements sucha s textboxes & graphical elements.
        the function expects a range of parameters which need to be tuned.

        TODO: sort out non-table area regions after every iteration and speed up subsequent
              table search iterations this way.. But optimize this on a recall-basis
              in order to make sure we don't sort out any valid tables...
        """
        boxes = pd.concat([
            self.df_le[box_cols] if "t" in self.tbe.extraction_method else pd.DataFrame(),
            self.df_ge_f[box_cols] if "g" in self.tbe.extraction_method else pd.DataFrame(),
        ])
        if len(boxes) == 0:
            return pd.DataFrame(), []

        box_levels = []

        # TODO: if (graphic) boxes are empty, revert to text-based...
        # TODO: do this in several (configurable) iterations
        if not self.tbe.area_detection_distance_func_params:
            raise ValueError("no area_detection_distance_func_params defined!")
        if len(boxes) > 1:  # merge boxes to table areas..
            for level, param_level in enumerate(self.tbe.area_detection_distance_func_params):
                x = gu.calc_pairwise_matrix(
                    gu.pairwise_weighted_distance_combination, boxes.values, diag=0,
                    parameter_list=param_level
                )

                boxes["groups"], dist_m = gu.distance_cluster(
                    distance_matrix=x, distance_threshold=self.distance_threshold
                )
                # create new column with the type of group (hb,hm,ht,vb,vm,vt) and their labels
                boxes = gu.merge_bbox_groups(boxes, "groups")
                box_levels.append(boxes)
                if len(boxes) < 2:
                    break

        # TODO: check variance to sort out "bad" columns as an additional parameter?
        #       but maybe it would also be a good idea to simply do that during the distance calculation
        # line_groups.groupby('vh_left_top_group').x0.apply(lambda x: x.var())

        # filter our empty groups
        # TODO: right now, we don't really know what would be a good filter...
        #       maybe do this by using an optimization approach
        table_groups = _filter_boxes(
            boxes,
            min_area=self.tbe.min_table_area,
            min_aspect_ratio=self.tbe.min_aspect_ratio,
            max_aspect_ratio=self.tbe.max_aspect_ratio
        )

        # sort table candidates according to y-coordinates top-to-bottom
        table_groups = table_groups.sort_values(
            by=["y1", "x0", "y0", "x1"], ascending=[False, True, False, True])

        return table_groups, box_levels

    @property
    def table_candidates(self) -> typing.List["PDFTable"]:
        tables = [PDFTable(parent_page=self, initial_area=row[box_cols]) for _, row in
                  self.detect_table_area_candidates()[0].iterrows()]
        # we only have a valid table if there is actualy text to be processed...
        # TODO:  also handly tables with figures only at some point in the future?
        # TODO: should we sort out "bad" areas here already? may speed up table extraction...
        return [t for t in tables if not t.df_le.empty]

    @property
    def table_candidate_boxes_df(self) -> pd.DataFrame:
        return pd.DataFrame([t.bbox for t in self.table_candidates])


class PDFTable:
    def __init__(self, parent_page: PDFPage, initial_area: np.ndarray):
        self.parent_page: PDFPage = parent_page
        self.initial_area: np.ndarray = initial_area

        self.max_lines = 1000

        self._debug = {}

    @property
    def tbe(self) -> TableExtractionParameters:
        return self.parent_page.tbe

    @cached_property
    def df_le(self) -> pd.DataFrame:
        """line elements of table"""
        return gu.boundarybox_query(
            self.parent_page.df_le, self.initial_area,
            tol=self.tbe.text_extraction_margin
        ).copy()

    @cached_property
    def df_ch(self) -> pd.DataFrame:
        """access to individual characters of the table. We can use this
        to apply our own layout algorithm, because the standard pdfminer.six
        algorithms don't work as well here...
        """
        return _LTObj2Chars(self.df_le)

    @cached_property
    def df_words(self):
        """Calculate word boxes instead of line boxes for tables
        this is important here, as we would like textboxes to be split according to detected
        cells. the standard algorithms of pdfminer.six don't do this
        as well...
        """

        char_dist_matrix = gu.calc_pairwise_matrix(
            pairwise_txtbox_dist,
            self.df_ch[box_cols].values,
            diag=0,
            min_line_alignement=self.tbe.max_char_disalignement,
            max_box_gap=self.tbe.max_char_dist
        )

        max_word_distance = 1.0
        self.df_ch["groups"], dist_m = gu.distance_cluster(
            distance_matrix=char_dist_matrix, distance_threshold=max_word_distance
        )
        # create new column with the type of group (hb,hm,ht,vb,vm,vt) and their labels
        bb_groups, group_sizes = gu.merge_groups(self.df_ch, "groups")

        # char_list = [[] for c in bb_groups]
        word_boxes = pd.DataFrame([
            (  # calculate new boundingbox
                g[:, 1].min(0),
                g[:, 2].min(0),
                g[:, 3].max(0),
                g[:, 4].min(0),
                g[g[:, 1].argsort()][:, 0]  # sort char boxes in the right order
            ) for g in bb_groups],
            columns=["x0", "y0", "x1", "y1", "chars"])
        word_boxes["text"] = word_boxes.chars.apply(lambda x: _line2txt(x).strip())

        return word_boxes

    @cached_property
    def df_ge(self) -> pd.DataFrame:
        """graphic elements of table"""
        # TODO: maybe we should not use filtered, but unfiltered graphic elements here?
        return gu.boundarybox_query(
            self.parent_page.df_ge_f, self.initial_area,
            tol=self.tbe.text_extraction_margin
        ).copy()

    @cached_property
    def bbox(self):
        # find the exact bounding box of our table...
        ge_bb = np.array([self.df_ge.x0.min(), self.df_ge.y0.min(), self.df_ge.x1.max(), self.df_ge.y1.max()])
        le_bb = np.array([self.df_le.x0.min(), self.df_le.y0.min(), self.df_le.x1.max(), self.df_le.y1.max()])
        return np.array([*np.vstack([ge_bb[:2], le_bb[:2]]).min(0), *np.vstack([ge_bb[2:], le_bb[2:]]).max(0)])

    @functools.lru_cache()
    def detect_cells(self, steps=None) -> pd.DataFrame:
        """
        This algorithm works by slowly scanning through a table bottom-to-top first
        and left-to-right for each row.

        On the way we detect rows by alternating between text lines and graphical lines.
        We can detect a cell border by going from the bottom of a text-line-box to the botom
        of the next graphical element box in that specific cell in both directions.
        All other textlines that are in between there should be part of that same cell.

        Additionally, we can efficiently assign textlines to cell areas while iterating
        through the rows.

        Once we have identified all cells and their borders
        we can infer their cell indices. By clustering their cell borders and identifying
        cells indices afterwards.

        TODO: add an ascii sketch about the process...

        TODO: we need to do something in the case where we have a table with actual
              box elements as table-rows/borders. in that case we can not use x0/x1 anymore..
        """

        max_v_line_thickness = self.tbe.max_v_line_thickness  # maximum thickness which is allowed for a vertical line
        elem_scan_tol = self.tbe.elem_scan_tol
        min_cell_width = self.tbe.min_cell_width
        text_line_tol = self.tbe.text_line_tol  # tolerance which detremines which text lines will be added to a cell area

        # TODO: use the "area-ratio" of elments in order to improve quality?
        # create indices for our graphical and line elements for
        # faster spatial queries
        # TODO: we might not need "w" and "h" or other elements...
        ge = self.df_ge[box_cols + ['w', 'h']].sort_values(
            by=['y0', 'y1', 'x0', 'x1'],
        ).set_index(['y0', 'y1', 'x0', 'x1'], drop=False).copy()
        le = self.df_words[box_cols + ['text']].sort_values(
            by=["y0", "x0", "y1", "x1"],
        ).set_index(['y0', 'x0', 'y1', 'x1'], drop=False).copy()

        # generate a list of increasing y-mean values which we can "pop"
        # from the list in order to scan the table upwards...
        y_mean = (le.y1 + le.y0) / 2.0
        y_mean_list = y_mean.drop_duplicates().sort_values(ascending=False).to_list()

        open_cells = []  # cells that the cursor is currently traversing
        # finished cells, where the y0-scan cursor has passed
        # by the top y1 border and "closed" them this way...
        cells = []

        # move through the table bottom-to-top evaluating row-by-row
        # define a y- and x-cursor which we will slowly advance in order to scan the table
        # we start with the first text element in the lower left
        y0_h_elem = 0.0

        max_steps = steps or self.max_lines  # maximum of 1000 lines for a table
        for i in range(max_steps):
            if not y_mean_list:  # if y_mean_list is empty, we reached the top of the table
                # as there are no more text lines we can close up all cells
                # that are still open
                last_cells = [_close_cell(oc, le, self.bbox[y1], text_line_tol) for oc in open_cells]
                cells.extend(lc for lc in last_cells if lc)
                break

            # advance y0_cursor to the next textline
            # TODO: use y_mean to the the left_over_lines!!
            while y_mean_list:  # do this until we don't have any more text elements left
                y0_cursor = y_mean_list.pop()
                # make sure that the middle of the line is above the previously found table row
                if y0_cursor > y0_h_elem:
                    break

            # now we would like to know every vertical line (not element) that crosses the current
            # y0-cursor-line to get the vertical cell borders in this row...
            # get all elements reaching from `below` to `above + tolerance` (using y0 and y1)
            # we do "swaplevel" in order to be able to use more efficient x-indexing afterwards
            row_ge_elem = ge.loc[idx[:y0_cursor + elem_scan_tol, y0_cursor + elem_scan_tol:], :]
            v_row_elem = row_ge_elem.swaplevel(0, 2, axis=0).sort_index()

            if v_row_elem.empty:
                # no vertical line detected in this row, so we should move further upwards
                # and set y0_h_elem to the top of this line element.
                if y_mean_list:
                    y0_h_elem = (y0_cursor + y_mean_list[-1]) / 2
                continue

            # get all vertical coordinates because we would like to identify the lines and not
            # boxes...
            vlines = np.sort(np.hstack(
                (np.unique(v_row_elem[['x0', 'x1']].values), self.bbox[[x0, x1]])))
            x0_cursor = vlines[0]
            for x in vlines:
                # TODO: take txt_lines  here into account. For example:
                #       if a cell has a textline which is longer than its x1-border,
                #       we should make the cell longer...
                if x < x0_cursor + min_cell_width:
                    x0_cursor = x
                    continue

                # check if this cell already exists in "open cells"
                # by checking if our x-cursor is inside that cell
                # if not, create it..
                if not next((oc for oc in open_cells
                             if oc['x0'] <= (x0_cursor + elem_scan_tol) <= oc['x1']), None):
                    # set cell borders but leave cells open as we have to advance our y0 cursor
                    # to find out the y1 value of the cells ("close" them)...
                    cell = dict(
                        x0=x0_cursor,
                        y0=y0_h_elem,
                        x1=x  # use left side of broder element as right side of cell
                    )
                    open_cells.append(cell)
                x0_cursor = x  # use right side as the next x0_cursor for left-side of the next cell

            # now after scanning the row, advance y-cursor upwards and check which cells we can close...
            # get the next horizontal element
            # TODO: handle the case where we have text "above" graphic lines...
            # TODO: might need to check for y1 here as well, as there
            #       might be a case were the box only closes and no new
            #       one opens... more tests will show...
            #       maybe check whatever is between here and the next text element?
            #       and then take the minimum y-coordinate from that..
            #       YEAH --> we need this.. already our first test showed :P
            left_over_y1 = ge.loc[idx[:, y0_cursor:], :]
            if left_over_y1.empty:
                # assume we are in the last box and need to
                # close up any remaining boxes...
                continue
            left_over_y0 = left_over_y1[y0_cursor:]
            next_h_elem = left_over_y1.iloc[0]
            # TODO: do we need a min here?
            y0_h_elem = next_h_elem.y1 if left_over_y0.empty else min(next_h_elem.y1, left_over_y0.iloc[0].y0)
            # TODO: we might want to check if *max_v_line_thickness* should be the
            #       distance to the next text element.... but maybe its enough to simply
            #       have this as a parameter...
            # select horizontal elements in row
            h_row_elem = left_over_y1.loc[
                         idx[: y0_h_elem + max_v_line_thickness,
                         y0_h_elem - elem_scan_tol:],
                         :]
            h_row_elem = h_row_elem.loc[
                h_row_elem.w > min_cell_width]  # .reorder_levels([2, 3, 0, 1], axis=0).sort_index()
            # and extract horizontal lines from them
            h_lines = pd.concat([
                h_row_elem.loc[y0_h_elem - elem_scan_tol:y0_h_elem + max_v_line_thickness,
                ["x0", "x1", "y0"]].rename(columns={"y0": "y"}),
                h_row_elem.loc[idx[:, y0_h_elem - elem_scan_tol:y0_h_elem + max_v_line_thickness],
                               ["x0", "x1", "y1"]].rename(columns={"y1": "y"})
            ]).droplevel(["y0", "y1"]).sort_index().drop_duplicates(["x0", "x1"])

            new_cells, still_open = _close_open_cells(
                open_cells, h_lines, le, elem_scan_tol, text_line_tol, y0_h_elem)
            open_cells = still_open
            cells.extend(new_cells)

        if steps:  # return additional debug information
            self._debug["open_cells"] = pd.DataFrame(open_cells)
            self._debug["open_cells"]['y1'] = self.bbox[y1]
            return pd.DataFrame(cells)
        else:
            return pd.DataFrame(cells)

    @functools.lru_cache()
    def convert_cells_to_df(self) -> typing.Tuple[pd.DataFrame, typing.Tuple]:
        """
        TODO: make the algorithm work with non-graphical tables as well by searching for rows/columns/cells
              using textboxes only.

        TODO: enhance function by also taking textboxes into account. (for large table cells)
        """
        """TODO: is the "loop"-approahc faster?
        
        
        #table = np.empty((len(hlines)-1,len(vlines)-1), dtype=object)
        #x_cells = (cell.x0<vlines[1:]) & (cell.x1>vlines[:-1])
        #y_cells = (cell.y0<hlines[1:]) & (cell.y1>hlines[:-1])[::-1] # reversed because we want table coordinates to be from top to bottom
        #table[y_cells,x_cells] = cell_text
        """

        cells = self.detect_cells().copy()
        if cells.empty or len(cells) < 2:
            return pd.DataFrame(), ([], [], cells)
        cells['text'] = cells["text_elements"].apply(_get_cell_text)

        # first thing we need to do is to cluster cell borders
        x_coordinates = cells[["x0", "x1"]].values
        y_coordinates = cells[["y0", "y1"]].values

        vlines = gu.cluster1D(x_coordinates.reshape(-1, 1), np.mean, self.tbe.table_line_merge_tol)
        hlines = gu.cluster1D(y_coordinates.reshape(-1, 1), np.mean, self.tbe.table_line_merge_tol)

        xtcs = _get_cell_coordinates(x_coordinates, vlines, tol=self.tbe.cell_idx_tol)
        ytcs = _get_cell_coordinates(y_coordinates, hlines, tol=self.tbe.cell_idx_tol)

        # --> sometimes we create "weired" vertical/horzontal lines by averaging them.
        # in that case some cells don't fit into the raste anymore and we have too few lines for every
        # cell. usually this happens in areas that are not real tables such as figures...
        # so this is a rally nice way to sort som of them out ;).
        # TODO: maybe at some poin in the future we need a more robust method to create
        #       coordinate grids though ...
        if (len(xtcs) != len(ytcs)) or (len(xtcs) < len(cells)) or (len(ytcs) < len(cells)):
            return pd.DataFrame(), ([], [], cells)

        cells['coords'] = [np.stack(np.meshgrid(xtc, ytc), -1).reshape(-1, 2) for xtc, ytc in zip(xtcs, ytcs)]
        t = cells[['coords', 'text']].explode("coords")
        t[['x', 'y']] = t.coords.apply(pd.Series)
        table = pd.pivot_table(t, values='text',
                               index=['y'], columns=['x'],
                               aggfunc='first', fill_value='')
        table = table.sort_values('y', ascending=False).reset_index(drop=True)
        return table, (hlines, vlines, cells)

    @cached_property
    def df(self):
        """get table as a pandas dataframe"""
        try:
            table, _ = self.convert_cells_to_df()
            return table
        except Exception as e:
            logger.error(self.identify_table())
            raise e

    @cached_property
    def metrics(self) -> typing.Dict[str, typing.Any]:
        """standard metrics only consist of the most necessary data
        in order to label pdf tables for parametr optimization"""
        table, (hlines, vlines, cells) = self.convert_cells_to_df()
        # TODO: over time calculate better metrics by goind
        #       through our table extractions and check wrong classifications

        # remove some special characters and normalize whitespace
        # to get a good md5 sum for these tables...
        tokentable = table.replace({'<.?s>': '', '\s+': ' '}, regex=True)
        tablestr = str(tokentable.values.tolist())
        md5 = hashlib.md5(tablestr.encode('utf-8')).hexdigest()

        metrics = dict(
            area_detection_method=self.tbe.extraction_method,
            table_area=self.bbox,
            md5=md5,
            x0=self.bbox[0],
            y0=self.bbox[1],
            x1=self.bbox[2],
            y1=self.bbox[3],
            page=self.parent_page.pagenum,
            page_bbox=self.parent_page.page_bbox,
            table=table,
            file=self.parent_page.parent_document.filename
        )

        return metrics

    def identify_table(self):
        return (f"Table {self.bbox} on page {self.parent_page.pagenum}, "
                f"{self.parent_page.parent_document.filename} with "
                f"index: {self.parent_page.pagenum - 1} can not be extracted.")

    @cached_property
    def is_valid(self):
        # TODO: include all the other table methods to check for validity...
        #       such as filter_correct_finished_tables
        try:
            if any((self.df_le.empty,
                    self.detect_cells().empty,
                    self.df.size <= 1)):
                return False
            else:
                # this function was generated by sklearn decision tree on our test dataset using
                # generate_pdf_table_features.py
                table, (hlines, vlines, cells) = self.convert_cells_to_df()
                words = self.df_words
                table_area = self.bbox

                words["w"] = words.eval("x1-x0")
                words["h"] = words.eval("y1-y0")
                words["area"] = words.eval("w*h")
                words_area_sum = words.area.sum()

                y0 = table_area[1]
                y1 = table_area[3]
                h = (y1 - y0)

                words_area_sum = words_area_sum
                cells_num = table.size  # number of cells in table
                cells_detected_num = len(cells)
                vlines_num = len(vlines)
                h = h

                if "coords" in cells:
                    cells_span_num = (cells.coords.str.len() > 1).sum()
                else:
                    return False

                if ((vlines_num / cells_detected_num) + (cells_span_num / cells_detected_num)) <= 1.2666667699813843:
                    if ((words_area_sum / h) - (h / cells_num)) <= 11.474941492080688:
                        return False  # classification scores: [[0.01 0.  ]]
                    else:  # if ((words_area_sum/h)-(h/cells_num)) > 11.474941492080688
                        return True  # classification scores: [[2.20e-02 3.59e+02]]
                else:  # if ((vlines_num/cells_detected_num)+(cells_span_num/cells_detected_num)) > 1.2666667699813843
                    return False  # classification scores: [[0.022 0.   ]]
        except Exception as e:
            logger.error(self.identify_table())
            raise e

    @property
    def metrics_X(self):
        """
        calculate some extended characteristics about the tables
        This helps in situations where we want to find better
        hyperparameters or classifiy tables (e.g. good/bad)...

        TODO: get max density in histogram and use that as a metric
              get the variance of the histogram and use that as a metric...
              tables should hae a pretty low variance, as the elements
              are distributed more equally than with a figure


        """
        try:
            table, (hlines, vlines, cells) = self.convert_cells_to_df()
            words = self.df_words
            table_area = self.bbox

            metrics = self.metrics
            words["w"] = words.eval("x1-x0")
            words["h"] = words.eval("y1-y0")
            words["area"] = words.eval("w*h")
            words_area_sum = words.area.sum()

            x0 = table_area[0]
            y0 = table_area[1]
            x1 = table_area[2]
            y1 = table_area[3]
            w = (x1 - x0)
            h = (y1 - y0)
            area = w * h

            empty_cells = table == ""

            if "text" in cells:
                word_count = cells.text.str.split().str.len().sum()
            else:
                word_count = 0

            metrics.update(dict(
                pageobj=self.parent_page,
                tableobj=self,
                words_area_sum=words_area_sum,
                word_line_num=words.size,
                word_count=word_count,
                table_area=area,
                table_text_len=len(table.values.sum()),
                cells_num=table.size,  # number of cells in table
                cells_detected_num=len(cells),
                hlines_num=len(hlines),
                vlines_num=len(vlines),
                table_line_count=len(self.df_le),
                table_word_count=len(self.df_words),
                graphic_elem_count=len(self.df_ge),
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                w=w,
                h=h,
                area=area,
                row_count=table.shape[0],
                col_count=table.shape[1],
                empty_cells_sum=empty_cells.values.sum(),
                empty_cols_count=empty_cells.any(1).sum(),
                empty_rows_count=empty_cells.any(0).sum(),

                # conditional metrics
                cell_detected_size_sum=0,
                cells_span_num=0,  # cells that span more than one cell in the grid
            ))

            if "coords" in cells:
                metrics.update(dict(
                    cell_detected_size_sum=cells.coords.str.len().sum(),
                    cells_span_num=(cells.coords.str.len() > 1).sum(),  # cells that span more than one cell in the grid
                ))
            return metrics
        except Exception as e:
            logger.error(self.identify_table())
            raise e

        ## calculate more statistics about table graphics

        try:
            points = gu.boundarybox_query(tm.pageobj.df_ge, bbox, tol=10)
            a, b = _calculate_ge_points_density_metrics(
                points[["y0", "y1"]].values.flatten())

            # point-density
            hist_resolution = 3  # [px]@72dpi pdf standard resolution
            bins = int(h / hist_resolution)
            ypoints = points[["y0", "y1"]].values.flatten()
            ypoints_hist = np.histogram(ypoints, bins=bins)[0]

            length = np.sqrt(points.w ** 2 + points.h ** 2)
            length_hist = np.histogram(length)

            # find max number of consecutive zeros
            # which equals the largest *gap* in graphic elements coordinates in the histogram
            max_gap = np.diff(np.where(ypoints_hist != 0)[0]).max()
            m2 = pd.Series(dict(
                density_var=b.var(),
                density_norm_var=b.var() / b.mean(),
                density_func=(a, b),
                density_max=b.max(),
                density_min=b.min(),
                density_min_max=b.max() - b.min(),
                density_min_max_norm=(b.max() - b.min()) / b.mean(),
                # density_mean=
                num_points=len(points),
                max_points_bin=ypoints_hist.max(),
                point_bin_var=ypoints_hist.var(),
                point_bin_notnull_var=ypoints_hist[ypoints_hist > 0].var(),
                ypoints_hist=ypoints_hist,
                point_density3=(ypoints_hist > 0).sum() / h,
                point_density=(ypoints_hist > 0).sum() / len(ypoints_hist),
                max_gap=max_gap,
                max_gap_perc=max_gap / ypoints_hist.size
                # len_group_density = point_groups.iloc[0]/row.area*100,
                # distribution = len(point_groups)/len(points),
                # max_regularity = point_groups.iloc[0]/len(points),
                # max3dist = point_groups.iloc[:3].sum()/len(point_groups)
            ))  # .mul(100).astype(int)

            return pd.concat([tm, m1, m2])
        except:  # something didn't work maybe there are no graphics elements?
            return pd.concat([tm, m1])
