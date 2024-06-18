"""Tests for utility functions in speech_to_text.py."""

from collections import namedtuple
import tempfile
import time
from unittest.mock import MagicMock, patch
from absl.testing import absltest
from absl.testing import parameterized
from ariel import speech_to_text
from faster_whisper import WhisperModel
import google.generativeai as genai
from google.generativeai.types import file_types
from moviepy.audio.AudioClip import AudioArrayClip
import numpy as np


class TranscribeTests(absltest.TestCase):

  def test_transcribe(self):
    with tempfile.NamedTemporaryFile(suffix=".mp3") as temporary_file:
      silence_duration = 5
      fps = 44100
      silence = AudioArrayClip(
          np.zeros((int(fps * silence_duration), 2), dtype=np.int16),
          fps=fps,
      )
      silence.write_audiofile(temporary_file.name)
      mock_model = MagicMock(spec=WhisperModel)
      Segment = namedtuple("Segment", ["text"])
      mock_model.transcribe.return_value = [Segment(text="Test.")], None
      transcribed_text = speech_to_text.transcribe(
          vocals_filepath=temporary_file.name,
          advertiser_name="Advertiser Name",
          original_language="en",
          model=mock_model,
      )
      self.assertEqual(
          transcribed_text,
          "Test.",
      )

  def test_transcribe_chunks(self):
    with tempfile.NamedTemporaryFile(suffix=".mp3") as temporary_file:
      silence_duration = 5
      fps = 44100
      silence = AudioArrayClip(
          np.zeros((int(fps * silence_duration), 2), dtype=np.int16),
          fps=fps,
      )
      silence.write_audiofile(temporary_file.name)
      mock_model = MagicMock(spec=WhisperModel)
      Segment = namedtuple("Segment", ["text"])
      mock_model.transcribe.return_value = [Segment(text="Test.")], None
      transcribed_audio_chunks = speech_to_text.transcribe_audio_chunks(
          chunk_data_list=[dict(path=temporary_file.name, start=0.0, end=5.0)],
          advertiser_name="Advertiser Name",
          original_language="en",
          model=mock_model,
      )
      self.assertEqual(
          transcribed_audio_chunks,
          [dict(path=temporary_file.name, start=0.0, end=5.0, text="Test.")],
      )


class UploadToGeminiTest(absltest.TestCase):

  def test_upload_to_gemini(self):
    mock_file = MagicMock(spec=file_types.File)
    mock_file.display_name = "test_file.mp4"
    mock_file.uri = "gs://test-bucket/test_file.mp4"
    with patch.object(genai, "upload_file", return_value=mock_file) as mock_upload_file:
      file = speech_to_text.upload_to_gemini(video_path="test_path.mp4")
      self.assertEqual(
          [file.display_name, file.uri],
          ["test_file.mp4", "gs://test-bucket/test_file.mp4"],
      )
      mock_upload_file.assert_called_once_with(
            video_path="test_path.mp4", mime_type="video/mp4",
            filename="test_file.mp4"
        )


class WaitForFileActiveTest(absltest.TestCase):

  def test_wait_for_file_active_success(self):
    mock_file = MagicMock(spec=file_types.File)
    mock_file.state.name = speech_to_text._PROCESSING
    mock_file.name = "test_file.mp4"
    with patch("ariel.speech_to_text.wait_for_file_active", return_value=None):
      mock_file.state.name = speech_to_text._ACTIVE
      speech_to_text.wait_for_file_active(file=mock_file)
      self.assertEqual(mock_file.state.name, speech_to_text._ACTIVE)

  def test_wait_for_file_active_timeout(self):
    mock_file = MagicMock(spec=file_types.File)
    mock_file.state.name = speech_to_text._PROCESSING
    mock_file.name = "test_file.mp4"
    mock_get_file = MagicMock(return_value=mock_file)
    with patch("ariel.speech_to_text.genai.get_file", new=mock_get_file):
      with patch(
          "ariel.speech_to_text.wait_for_file_active"
      ) as mock_wait_for_file:
        mock_wait_for_file.side_effect = speech_to_text.FileProcessingError(
            "File 'test_file.mp4' failed to process."
        )
        with patch(
            "time.sleep", side_effect=lambda _: time.sleep(0.1)
        ) as mock_sleep:
          with self.assertRaisesRegex(
              speech_to_text.FileProcessingError,
              "File 'test_file.mp4' failed to process.",
          ):
            speech_to_text.wait_for_file_active(file=mock_file)


class TestProcessSpeakerDiarizationResponse(parameterized.TestCase):
    @parameterized.parameters([
        ("", []),
        ("(speaker_1, female)\n", [("speaker_1", "female")]),
    ])
    def test_process_speaker_diarization_response(
        self, response, expected_output
    ):
        self.assertEqual(
            speech_to_text.process_speaker_diarization_response(response=response),
            expected_output,
        )


class DiarizeSpeakersTest(absltest.TestCase):

  @patch("google.generativeai")
  @patch("ariel.speech_to_text.wait_for_file_active")
  @patch("ariel.speech_to_text.upload_to_gemini")
  def test_diarize_speakers(
      self, mock_upload_to_gemini, mock_wait_for_file_active, mock_genai
  ):
    video_path = "test_video.mp4"
    video_transcript = [
        {"start": 0.0, "stop": 5.0, "text": "Hello, this is a test video."},
        {"start": 5.0, "stop": 10.0, "text": "How are you?"},
    ]
    number_of_speakers = 2
    model = MagicMock(spec=genai.GenerativeModel)
    diarization_instructions = "Please be specific."

    mock_chat_session = MagicMock()
    mock_chat_session.send_message.return_value = MagicMock(
        text="(speaker_1, Male), (speaker_2, Female)"
    )
    mock_chat_session.rewind.return_value = None
    model.start_chat.return_value = mock_chat_session

    mock_file = MagicMock()
    mock_file.name = "test_video.mp4"
    mock_file.state = MagicMock(name="ACTIVE")
    mock_upload_to_gemini.return_value = mock_file

    result = speech_to_text.diarize_speakers(
        video_path=video_path,
        video_transcript=video_transcript,
        number_of_speakers=number_of_speakers,
        model=model,
        diarization_instructions=diarization_instructions,
    )

    self.assertEqual(result, [("speaker_1", "Male"), ("speaker_2", "Female")])
    mock_upload_to_gemini.assert_called_once_with(video_path=video_path)
    mock_wait_for_file_active.assert_called_once_with(file=mock_file)
    model.start_chat.assert_called_once_with(
        history=[{"role": "user", "parts": mock_file}]
    )
    mock_chat_session.send_message.assert_called_once_with(
        speech_to_text._DIARIZATION_PROMPT.format(
            video_transcript,
            number_of_speakers,
            len(video_transcript),
            diarization_instructions,
        )
    )
    mock_chat_session.rewind.assert_called_once()

  @patch("google.generativeai")
  @patch("ariel.speech_to_text.wait_for_file_active")
  @patch("ariel.speech_to_text.upload_to_gemini")
  def test_diarize_speakers_file_processing_error(
      self, mock_upload_to_gemini, mock_wait_for_file_active, mock_genai
  ):
    video_path = "test_video.mp4"
    video_transcript = [
        {"start": 0.0, "stop": 5.0, "text": "Hello, this is a test video."},
        {"start": 5.0, "stop": 10.0, "text": "How are you?"},
    ]
    number_of_speakers = 2
    model = MagicMock(spec=genai.GenerativeModel)

    mock_file = MagicMock()
    mock_file.name = "test_video.mp4"
    mock_file.state = MagicMock(name="PROCESSING")
    mock_upload_to_gemini.return_value = mock_file

    mock_wait_for_file_active.side_effect = speech_to_text.FileProcessingError(
        "File processing failed."
    )

    with self.assertRaises(speech_to_text.FileProcessingError) as context:
      speech_to_text.diarize_speakers(
          video_path=video_path,
          video_transcript=video_transcript,
          number_of_speakers=number_of_speakers,
          model=model,
      )

    self.assertEqual(str(context.exception), "File processing failed.")
    mock_upload_to_gemini.assert_called_once_with(video_path=video_path)
    mock_wait_for_file_active.assert_called_once_with(file=mock_file)


class AddSpeakerInfoTest(absltest.TestCase):

  def test_add_speaker_info(self):
    utterance_metadata = [
        {"text": "Hello", "start": 0.0, "stop": 1.0},
        {"text": "world", "start": 1.0, "stop": 2.0},
    ]
    speaker_info = [("speaker1", "male"), ("speaker2", "female")]
    expected_result = [
        {
            "text": "Hello",
            "start": 0.0,
            "stop": 1.0,
            "speaker_id": "speaker1",
            "ssml_gender": "male",
        },
        {
            "text": "world",
            "start": 1.0,
            "stop": 2.0,
            "speaker_id": "speaker2",
            "ssml_gender": "female",
        },
    ]
    result = speech_to_text.add_speaker_info(utterance_metadata, speaker_info)
    self.assertEqual(result, expected_result)

  def test_add_speaker_info_unequal_lengths(self):
    utterance_metadata = [
        {"text": "Hello", "start": 0.0, "stop": 1.0},
        {"text": "world", "start": 1.0, "stop": 2.0},
    ]
    speaker_info = [("speaker1", "male")]
    with self.assertRaisesRegex(
        ValueError,
        "The length of 'utterance_metadata' and 'speaker_info' must be the"
        " same.",
    ):
      speech_to_text.add_speaker_info(utterance_metadata, speaker_info)


if __name__ == "__main__":
  absltest.main()
