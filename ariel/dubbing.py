"""A dubbing module of Ariel package from the Google EMEA gTech Ads Data Science."""

import dataclasses
import functools
import json
import os
import pathlib
import shutil
import time
from typing import Final, Mapping, Sequence
from absl import logging
from ariel import audio_processing
from ariel import speech_to_text
from ariel import text_to_speech
from ariel import translation
from ariel import video_processing
from faster_whisper import WhisperModel
from google.cloud import texttospeech
import google.generativeai as genai
from google.generativeai.types import HarmBlockThreshold, HarmCategory
from pyannote.audio import Pipeline
import torch
from tqdm import tqdm
import tensorflow as tf


_ACCEPTED_VIDEO_FORMATS: Final[tuple[str, ...]] = (".mp4",)
_ACCEPTED_AUDIO_FORMATS: Final[tuple[str, ...]] = (".wav", ".mp3", ".flac")
_UTTERNACE_METADATA_FILE_NAME: Final[str] = "utterance_metadata.json"
_EXPECTED_HUGGING_FACE_ENVIRONMENTAL_VARIABLE_NAME: Final[str] = "HUGGING_FACE_TOKEN"
_EXPECTED_GEMINI_ENVIRONMENTAL_VARIABLE_NAME: Final[str] = "GEMINI_TOKEN"
_DEFAULT_PYANNOTE_MODEL: Final[str] = "pyannote/speaker-diarization-3.1"
_DEFAULT_TRANSCRIPTION_MODEL: Final[str] = "large-v3"
_DEFAULT_GEMINI_MODEL: Final[str] = "gemini-1.5-flash"
_DEFAULT_GEMINI_TEMPERATURE: Final[float] = 1.0
_DEFAULT_GEMINI_TOP_P: Final[float] = 0.95
_DEFAULT_GEMINI_TOP_K: Final[int] = 64
_DEFAULT_GEMINI_MAX_OUTPUT_TOKENS: Final[int] = 8192
_DEFAULT_GEMINI_RESPONSE_MIME_TYPE: Final[str] = "text/plain"
_DEFAULT_GEMINI_SAFETY_SETTINGS: Final[
    Mapping[HarmCategory, HarmBlockThreshold]
] = {
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: (
        HarmBlockThreshold.BLOCK_LOW_AND_ABOVE
    ),
    HarmCategory.HARM_CATEGORY_HARASSMENT: (
        HarmBlockThreshold.BLOCK_LOW_AND_ABOVE
    ),
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: (
        HarmBlockThreshold.BLOCK_LOW_AND_ABOVE
    ),
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: (
        HarmBlockThreshold.BLOCK_LOW_AND_ABOVE
    ),
}
module_path = pathlib.Path(__file__).parent.resolve()
_DEFAULT_DIARIZATION_SYSTEM_SETTINGS: Final[str] = os.path.join(
    module_path.parent, "assets", "system_settings_diarization.txt"
)
_DEFAULT_TRANSLATION_SYSTEM_SETTINGS: Final[str] = os.path.join(
    module_path.parent, "assets", "system_settings_translation.txt"
)
_NUMBER_OF_STEPS: Final [int] = 6


def is_video(*, input_file: str) -> bool:
  """Checks if a given file is a video (MP4) or audio (WAV, MP3, FLAC).

  Args:
      input_file: The path to the input file.

  Returns:
      True if it's an MP4 video, False otherwise.

  Raises:
      ValueError: If the file format is unsupported.
  """

  _, file_extension = os.path.splitext(input_file)
  file_extension = file_extension.lower()

  if file_extension in _ACCEPTED_VIDEO_FORMATS:
    return True
  elif file_extension in _ACCEPTED_AUDIO_FORMATS:
    return False
  else:
    raise ValueError(f"Unsupported file format: {file_extension}")


def read_system_settings(system_instructions: str) -> str:
  """Reads a .txt file with system instructions or returns them directly.

  - If it's a .txt file, reads and returns the content. - If it has another
  extension, raises a ValueError. - If it's just a string, returns it as is.

  Args:
      system_instructions: The string to process.

  Returns:
      The content of the .txt file or the input string.

  Raises:
      ValueError: If the input has an unsupported extension.
      TypeError: If the input file doesn't exist.
      FileNotFoundError: If the .txt file doesn't exist.
  """
  if not isinstance(system_instructions, str):
    raise TypeError("Input must be a string")

  _, extension = os.path.splitext(system_instructions)

  if extension == ".txt":
    try:
      with tf.io.gfile.GFile(system_instructions, "r") as file:
        return file.read()
    except FileNotFoundError:
      raise FileNotFoundError(f"File not found: {system_instructions}")
  elif extension:
    raise ValueError(f"Unsupported file type: {extension}")
  else:
    return system_instructions


@dataclasses.dataclass
class PreprocessingArtifacts:
  """Instance with preprocessing outputs.

  Attributes:
      video_file: A path to a video ad with no audio.
      audio_file: A path to an audio track from the ad.
      audio_background_file: A path to and audio track from the ad with removed
        vocals.
      utterance_metadata: The sequence of utterance metadata mappings. Each
        dictionary represents a chunk of audio and contains the "path", "start",
        "stop" keys.
  """

  video_file: str
  audio_file: str
  audio_background_file: str
  utterance_metadata: Sequence[Mapping[str, str | float]]


class Dubber:
  """A class to manage the entire ad dubbing process."""

  def __init__(
      self,
      *,
      input_file: str,
      output_directory: str,
      advertiser_name: str,
      original_language: str,
      target_language: str,
      number_of_speakers: int = 1,
      diarization_instructions: str | None = None,
      translation_instructions: str | None = None,
      merge_utterances: bool = True,
      minimum_merge_threshold: float = 0.001,
      preferred_voices: Sequence[str] | None = None,
      clean_up: bool = True,
      pyannote_model: str = _DEFAULT_PYANNOTE_MODEL,
      diarization_system_instructions: str = _DEFAULT_DIARIZATION_SYSTEM_SETTINGS,
      translation_system_instructions: str = _DEFAULT_TRANSLATION_SYSTEM_SETTINGS,
      hugging_face_token: str | None = None,
      gemini_token: str | None = None,
      model_name: str = _DEFAULT_GEMINI_MODEL,
      temperature: float = _DEFAULT_GEMINI_TEMPERATURE,
      top_p: float = _DEFAULT_GEMINI_TOP_P,
      top_k: int = _DEFAULT_GEMINI_TOP_K,
      max_output_tokens: int = _DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
      response_mime_type: str = _DEFAULT_GEMINI_RESPONSE_MIME_TYPE,
  ) -> None:
    """Initializes the Dubber class with various parameters for dubbing configuration.

    Args:
        input_file: The path to the input video or audio file.
        output_directory: The directory to save the dubbed output and
          intermediate files.
        advertiser_name: The name of the advertiser for context in
          transcription/translation.
        original_language: The language of the original audio. It must be ISO
          3166-1 alpha-2 country code.
        target_language: The language to dub the ad into. It must be ISO 3166-1
          alpha-2 country code.
        number_of_speakers: The exact number of speakers in the ad (including a
          lector if applicable).
        diarization_instructions: Specific instructions for speaker diarization.
        translation_instructions: Specific instructions for translation.
        merge_utterances: Whether to merge utterances when the the timestamps
          delta between them is below 'minimum_merge_threshold'.
        minimum_merge_threshold: Threshold for merging utterances in seconds.
        preferred_voices: Preferred voice names for text-to-speech. Use
          high-level names, e.g. 'Wavenet', 'Standard' etc. Do not use the full
          voice names, e.g. 'pl-PL-Wavenet-A' etc.
        clean_up: Whether to delete intermediate files after dubbing. Only the
          final ouput and the utterance metadata will be kept.
        pyannote_model: Name of the PyAnnote diarization model.
        diarization_system_instructions: System instructions for diarization.
        translation_system_instructions: System instructions for translation.
        hugging_face_token: Hugging Face API token (can be set via
          'HUGGING_FACE_TOKEN' environment variable).
        gemini_token: Gemini API token (can be set via 'GEMINI_TOKEN'
          environment variable).
        model_name: The name of the Gemini model to use.
        temperature: Controls randomness in generation.
        top_p: Nucleus sampling threshold.
        top_k: Top-k sampling parameter.
        max_output_tokens: Maximum number of tokens in the generated response.
    """
    self.input_file = input_file
    self.output_directory = output_directory
    self.advertiser_name = advertiser_name
    self.original_language = original_language
    self.target_language = target_language
    self.number_of_speakers = number_of_speakers
    self.diarization_instructions = diarization_instructions
    self.translation_instructions = translation_instructions
    self.merge_utterances = merge_utterances
    self.minimum_merge_threshold = minimum_merge_threshold
    self.preferred_voices = preferred_voices
    self.clean_up = clean_up
    self.pyannote_model = pyannote_model
    self.hugging_face_token = hugging_face_token
    self.gemini_token = gemini_token
    self.diarization_system_instructions = diarization_system_instructions
    self.translation_system_instructions = translation_system_instructions
    self.model_name = model_name
    self.temperature = temperature
    self.top_p = top_p
    self.top_k = top_k
    self.max_output_tokens = max_output_tokens
    self.response_mime_type = response_mime_type

  @functools.cached_property
  def device(self):
    return "gpu" if torch.cuda.is_available() else "cpu"

  @functools.cached_property
  def is_video(self) -> bool:
    """Checks if the input file is a video."""
    return is_video(input_file=self.input_file)

  def get_api_token(self, *, env_variable: str, provided_token: str | None = None) -> str:
    """Helper to get API token, prioritizing provided argument over environment variable.

    Args:
        env_variable: The name of the environment variable storing the API
          token.
        provided_token: The API token provided directly as an argument.

    Returns:
        The API token (either the provided one or from the environment).

    Raises:
        ValueError: If neither the provided token nor the environment variable
        is set.
    """
    token = provided_token or os.getenv(env_variable)
    if not token:
      raise ValueError(
          f"You must either provide the '{env_variable}' argument or set the"
          f" '{env_variable.upper()}' environment variable."
      )
    return token

  @property
  def pyannote_pipeline(self) -> Pipeline:
    """Loads the PyAnnote diarization pipeline."""
    hugging_face_token = self.get_api_token(
        _EXPECTED_HUGGING_FACE_ENVIRONMENTAL_VARIABLE_NAME, self.hugging_face_token
    )
    return Pipeline.from_pretrained(
        self.pyannote_model, use_auth_token=hugging_face_token
    )

  @property
  def speech_to_text_model(self) -> WhisperModel:
    """Initializes the Whisper speech-to-text model."""
    return WhisperModel(
        model_size_or_path=_DEFAULT_TRANSCRIPTION_MODEL,
        device=self.device,
        compute_type="float16" if self.device == "gpu" else "int8",
    )

  def configure_gemini_model(
      self, *, system_instruction: str
  ) -> genai.GenerativeModel:
    """Configures the Gemini generative model.

    Args:
        system_instruction: The system instruction to guide the model's
          behavior.
        model_name: The name of the Gemini model to use.
        temperature: Controls randomness in generation.
        top_p: Nucleus sampling threshold.
        top_k: Top-k sampling parameter.
        max_output_tokens: Maximum number of tokens in the generated response.
        response_mime_type: MIME type of the generated response.

    Returns:
        The configured Gemini model instance.
    """

    gemini_token = self.get_api_token(_EXPECTED_GEMINI_ENVIRONMENTAL_VARIABLE_NAME, self.gemini_token)
    genai.configure(api_key=gemini_token)
    gemini_configuration = dict(
        temperature=self.temperature,
        top_p=self.top_p,
        top_k=self.top_k,
        max_output_tokens=self.max_output_tokens,
        response_mime_type=self.response_mime_type,
    )
    return genai.GenerativeModel(
        model_name=self.model_name,
        generation_config=gemini_configuration,
        system_instruction=system_instruction,
        safety_settings=_DEFAULT_GEMINI_SAFETY_SETTINGS,
    )

  @property
  def text_to_speech_client(self) -> texttospeech.TextToSpeechClient:
    """Creates a Text-to-Speech client."""
    return texttospeech.TextToSpeechClient()

  @functools.cached_property
  def diarization_system_instructions(self) -> str:
    """Reads and caches diarization system instructions."""
    return read_system_settings(
        system_instructions=self.diarization_system_instructions
    )

  @functools.cached_property
  def translation_system_instructions(self) -> str:
    """Reads and caches translation system instructions."""
    return read_system_settings(
        system_instructions=self.translation_system_instructions
    )

  def run_preprocessing(self) -> PreprocessingArtifacts:
    """Splits audio/video, applies DEMUCS, and segments audio into utterances with PyAnnote.

    Returns:
        A named tuple containing paths and metadata of the processed files.
    """
    if self.is_video:
      video_file, audio_file = video_processing.split_audio_video(
          video_file=self.input_file, output_directory=self.output_directory
      )
    else:
      video_file = None
      audio_file = self.input_file

    demucs_command = audio_processing.build_demucs_command(
        audio_file=audio_file,
        output_directory=self.output_directory,
    )
    audio_processing.execute_demcus_command(command=demucs_command)
    _, audio_background_file = audio_processing.assemble_split_audio_file_paths(
        command=demucs_command
    )

    utterance_metadata = audio_processing.create_pyannote_timestamps(
        audio_file=audio_file,
        number_of_speakers=self.number_of_speakers,
        pipeline=self.pyannote_pipeline,
    )
    utterance_metadata = audio_processing.cut_and_save_audio(
        utterance_metadata=utterance_metadata,
        audio_file=audio_file,
        output_directory=self.output_directory,
    )
    logging.info("Completed preprocessing.")
    self.progress_bar.update(1)
    return PreprocessingArtifacts(
        video_file=video_file,
        audio_file=audio_file,
        audio_background_file=audio_background_file,
        utterance_metadata=utterance_metadata,
    )

  def run_speech_to_text(self) -> Sequence[Mapping[str, str | float]]:
    """Transcribes audio, applies speaker diarization, and updates metadata with Gemini.

    Returns:
        Updated utterance metadata with speaker information and transcriptions.
    """
    media_file = (
        self.preprocesing_output.video_file
        if self.preprocesing_output.video_file
        else self.preprocesing_output.audio_file
    )
    utterance_metadata = speech_to_text.transcribe_audio_chunks(
        utterance_metadata=self.preprocesing_output.utterance_metadata,
        advertiser_name=self.advertiser_name,
        original_language=self.original_language,
        model=self.speech_to_text_model,
    )

    speaker_diarization_model = self.configure_gemini_model(
        system_instructions=self.diarization_system_instructions
    )
    speaker_info = speech_to_text.diarize_speakers(
        file=media_file,
        utterance_metadata=utterance_metadata,
        model=speaker_diarization_model,
        diarization_instructions=self.diarization_instructions,
    )
    utterance_metadata = speech_to_text.add_speaker_info(
        utterance_metadata=utterance_metadata, speaker_info=speaker_info
    )
    logging.info("Completed transcription.")
    self.progress_bar.update(1)
    return utterance_metadata

  def run_translation(self) -> Sequence[Mapping[str, str | float]]:
    """Translates transcribed text and potentially merges utterances with Gemini.

    Returns:
        Updated utterance metadata with translated text.
    """
    script = translation.generate_script(utterance_metadata=self.speech_to_text_output)
    translation_model = self.configure_gemini_model(
        system_instructions=self.translation_system_instructions
    )
    translated_script = translation.translate_script(
        script=script,
        advertiser_name=self.advertiser_name,
        translation_instructions=self.translation_instructions,
        target_language=self.target_language,
        model=translation_model,
    )
    utterance_metadata = translation.add_translations(
        utterance_metadata=utterance_metadata,
        translated_script=translated_script,
    )
    if self.merge_utterances:
      utterance_metadata = translation.merge_utterances(
          utterance_metadata=utterance_metadata,
          minimum_merge_threshold=self.minimum_merge_threshold,
      )
    logging.info("Completed translation.")
    self.progress_bar.update(1)
    return utterance_metadata

  def run_text_to_speech(self) -> Sequence[Mapping[str, str | float]]:
    """Converts translated text to speech and dubs utterances with Google's Text-To-Speech.

    Returns:
        Updated utterance metadata with generated speech file paths.
    """

    assigned_voices = text_to_speech.assign_voices(
        utterance_metadata=self.translation_output,
        target_language=self.target_language,
        preferred_voices=self.preferred_voices,
    )
    utterance_metadata = text_to_speech.update_utterance_metadata(
        utterance_metadata=utterance_metadata, assigned_voices=assigned_voices
    )
    utterance_metadata = text_to_speech.dub_utterances(
        client=self.text_to_speech_client,
        utterance_metadata=utterance_metadata,
        output_directory=self.output_directory,
        target_language=self.target_language,
    )
    logging.info("Completed converting text to speech.")
    self.progress_bar.update(1)
    return utterance_metadata

  def run_postprocessing(self) -> str:
    """Merges dubbed audio with the original background audio and video (if applicable).

    Returns:
        Path to the final dubbed output file (audio or video).
    """

    dubbed_audio_vocals_file = audio_processing.insert_audio_at_timestamps(
        utterance_metadata=self.text_to_speech_output,
        background_audio_file=self.preprocesing_output.audio_background_file,
        output_directory=self.output_directory,
    )
    dubbed_audio_file = audio_processing.merge_background_and_vocals(
        background_audio_file=self.preprocesing_output.audio_background_file,
        dubbed_vocals_audio_file=dubbed_audio_vocals_file,
        output_directory=self.output_directory,
    )
    if self.is_video:
      if not self.preprocesing_output.video_file:
        raise ValueError(
            "A video file must be provided if the input file is a video."
        )
      output_file = video_processing.combine_audio_video(
          video_file=self.preprocesing_output.video_file,
          dubbed_audio_file=dubbed_audio_file,
          output_directory=self.output_directory,
      )
    else:
      output_file = dubbed_audio_file
    logging.info("Completed postprocessing.")
    self.progress_bar.update(1)
    return output_file

  def run_clean_directory(self, keep_files: Sequence[str]) -> None:
    """Removes all files and directories from a directory, except for those listed in keep_files.

    Args:
      keep_files: A sequence with files to keep.  
    """
    keep_files = [self.postprocessing_output, ]
    for filename in tf.io.gfile.listdir(self.output_directory):
      file_path = os.path.join(self.output_directory, filename)
      if filename in keep_files:
        continue
      if tf.io.gfile.exists(file_path):
        tf.io.gfile.remove(file_path)
      elif tf.io.gfile.isdir(file_path):
        shutil.rmtree(file_path)
    logging.info("Temporary artifacts are now removed.")
    self.progress_bar.update(1)

  def run_save_utterance_metadata(self) -> str:
    """Saves a Python dictionary to a JSON file.

    Returns:
      A path to the saved uttterance metadata.
    """
    utterance_metadata_file = os.path.join(
        self.output_directory, _UTTERNACE_METADATA_FILE_NAME
    )
    try:
      with tf.io.gfile.GFile(utterance_metadata_file, "w") as json_file:
        json.dump(self.text_to_speech_output, json_file)
      logging.info(
          f"Utterance metadata saved successfully to '{utterance_metadata_file}'"
      )
    except Exception as e:
      logging.warning(f"Error saving utterance metadata: {e}")
    return utterance_metadata_file

  @functools.cached_property
  def progress_bar(self):
    total_number_of_steps = _NUMBER_OF_STEPS if self.clean_up else _NUMBER_OF_STEPS - 1
    return tqdm(total=total_number_of_steps)

  @functools.cached_property
  def preprocesing_output(self):
    return self.preprocessing()

  @functools.cached_property
  def speech_to_text_output(self):
    return self.speech_to_text()

  @functools.cached_property
  def translation_output(self):
    return self.speech_to_text()

  @functools.cached_property
  def text_to_speech_output(self):
    return self.speech_to_text()

  @functools.cached_property
  def postprocessing_output(self):
    return self.postprocessing()

  def dub_ad(self) -> str:
    """Orchestrates the entire ad dubbing process."""
    logging.info("Dubbing process starting...")
    start_time = time.time()
    self.run_preprocessing()
    self.run_speech_to_text()
    self.run_translation()
    self.run_text_to_speech()
    self.run_save_utterance_metadata()
    self.run_postprocessing()
    if self.clean_up:
      self.run_clean_directory()
    logging.info("Dubbing process finished.")
    end_time = time.time()
    logging.info("Total execution time: %.2f seconds.", end_time - start_time)
    logging.info("Output file saved under: %s.", self.postprocessing_output)
    return self.postprocessing_output