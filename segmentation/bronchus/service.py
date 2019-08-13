import sys
sys.path.append('..')

from impit.framework import app, DataObject, celery
from impit.dicom.nifti_to_rtstruct.convert import convert_nifti

import SimpleITK as sitk
import pydicom
from loguru import logger
import os

from bronchus import GenLungMask, GenAirwayMask

bronchus_settings_defaults = {
    'outputContourName': 'Auto_Bronchus',
}
    
@app.register('Bronchus Segmentation', default_settings=bronchus_settings_defaults)
def bronchus_service(data_objects, working_dir, settings):

    logger.info('Running Bronchus Segmentation')
    logger.info('Using settings: ' + str(settings))

    output_objects = []
    for d in data_objects:
        logger.info('Running on data object: ' + d.path)

        # Read the image series
        load_path = d.path
        if d.type == 'DICOM':
            load_path = sitk.ImageSeriesReader().GetGDCMSeriesFileNames(d.path)

        img = sitk.ReadImage(load_path)

        # Compute the lung mask
        lung_mask = GenLungMask(img, working_dir)
        bronchus_mask = GenAirwayMask(working_dir, img, lung_mask)

        # If the bronchus mask counldn't be generated then skip it
        if not bronchus_mask:
            continue

        # Write the mask to a file in the working_dir
        mask_file = os.path.join(
            working_dir, '{0}.nii.gz'.format(settings['outputContourName']))
        sitk.WriteImage(bronchus_mask, mask_file)

        # Create the output Data Object and add it to the list of output_objects
        do = DataObject(type='FILE', path=mask_file, parent=d)
        output_objects.append(do)

        # If the input was a DICOM, then we can use it to generate an output RTStruct
        # if d.type == 'DICOM':

        #     dicom_file = load_path[0]
        #     logger.info('Will write Dicom using file: {0}'.format(dicom_file))
        #     masks = {settings['outputContourName']: mask_file}

        #     # Use the image series UID for the file of the RTStruct
        #     suid = pydicom.dcmread(dicom_file).SeriesInstanceUID
        #     output_file = os.path.join(working_dir, 'RS.{0}.dcm'.format(suid))

        #     # Use the convert nifti function to generate RTStruct from nifti masks
        #     convert_nifti(dicom_file, masks, output_file)

        #     # Create the Data Object for the RTStruct and add it to the list
        #     do = DataObject(type='DICOM', path=output_file, parent=d)
        #     output_objects.append(do)

        #     logger.info('RTStruct generated')

    return output_objects

if __name__ == "__main__":

    # Run app by calling "python sample.py" from the command line

    dicom_listener_port=7777
    dicom_listener_aetitle="SAMPLE_SERVICE"

    app.run(debug=True, host="0.0.0.0", port=8000,
        dicom_listener_port=dicom_listener_port,
        dicom_listener_aetitle=dicom_listener_aetitle)
