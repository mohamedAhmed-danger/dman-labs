/**
 * static/js/test_service.js
 * Clean vanilla JavaScript handling AI Auto-generation, manual Generate/Regenerate,
 * and Tag/Chip management for the Laboratory Test Service create/edit workflow.
 */

document.addEventListener('DOMContentLoaded', () => {
  initTestFormWorkflow();
  initImportModalWorkflow();
});

function initTestFormWorkflow() {
  const form = document.getElementById('testForm');
  if (!form) return;

  const isEditMode = form.dataset.isEdit === 'true';
  const nameInput = document.getElementById('testNameInput');
  const priceInput = document.getElementById('testPriceInput');
  const durationInput = document.getElementById('testDurationInput');
  const sampleInput = document.getElementById('testSampleTypeInput');
  const generateBtn = document.getElementById('generateBtn');
  const regenerateBtn = document.getElementById('regenerateBtn');

  // Tag inputs
  initTagInput('keywordsContainer', 'keywordsTextInput', 'keywords');
  initTagInput('aliasesContainer', 'aliasesTextInput', 'alias_name');

  // Track the last text used for generation to avoid redundant calls
  let lastGeneratedName = (nameInput && nameInput.value) ? nameInput.value.trim() : '';
  let isGenerating = false;
  let debounceTimer = null;

  // Smart condition for Auto-Generation
  function canTriggerAutoGenerate() {
    if (isEditMode) return false;
    
    const name = nameInput ? nameInput.value.trim() : '';
    const price = priceInput ? priceInput.value.trim() : '';
    const duration = durationInput ? durationInput.value.trim() : '';
    const sample = sampleInput ? sampleInput.value.trim() : '';

    // 1. Name (>3 chars) and Price are strictly required
    const hasCoreRequirements = name.length >= 3 && price !== '' && parseFloat(price) > 0;
    
    // 2. If the user manually wrote duration or sample type, skip auto-generation to respect manual input
    const userWroteExtra = duration !== '' || sample !== '';

    return hasCoreRequirements && !userWroteExtra && name !== lastGeneratedName && !isGenerating;
  }

  // 1. Automatic AI Generation on Test Name & Price Input (Create Mode only)
  if (!isEditMode && nameInput && priceInput) {
    const handleAutoTrigger = () => {
      clearTimeout(debounceTimer);
      if (canTriggerAutoGenerate()) {
        debounceTimer = setTimeout(() => {
          if (canTriggerAutoGenerate()) {
            triggerGenerate(true);
          }
        }, 2000); // 2 seconds delay after user stops typing
      }
    };

    nameInput.addEventListener('input', handleAutoTrigger);
    priceInput.addEventListener('input', handleAutoTrigger);

    // Cancel timer if user starts typing custom duration or sample type
    if (durationInput) durationInput.addEventListener('input', () => clearTimeout(debounceTimer));
    if (sampleInput) sampleInput.addEventListener('input', () => clearTimeout(debounceTimer));
  }

  // 2. Manual "Generate with AI" button
  if (generateBtn) {
    generateBtn.addEventListener('click', (e) => {
      e.preventDefault();
      triggerGenerate(false);
    });
  }

  // 3. Manual "Regenerate" button
  if (regenerateBtn) {
    regenerateBtn.addEventListener('click', (e) => {
      e.preventDefault();
      triggerRegenerate();
    });
  }

  // 4. Ensure any typed tag is saved when form is submitted
  form.addEventListener('submit', () => {
    commitPendingTag('keywordsContainer', 'keywordsTextInput', 'keywords');
    commitPendingTag('aliasesContainer', 'aliasesTextInput', 'alias_name');
  });

  // ── Generation Function ─────────────────────────────────────
  async function triggerGenerate(isAuto = false) {
    if (isGenerating) return;

    const name = nameInput ? nameInput.value.trim() : '';
    const price = priceInput ? priceInput.value.trim() : '';

    if (!name) {
      if (!isAuto) {
        showStatus('error', 'يرجى إدخال اسم التحليل الطبي أولاً لتوليد البيانات.');
        if (nameInput) nameInput.focus();
      }
      return;
    }

    if (!price && !isAuto) {
      showStatus('error', 'يرجى إدخال سعر التحليل الطبي أولاً.');
      if (priceInput) priceInput.focus();
      return;
    }

    isGenerating = true;
    lastGeneratedName = name;
    setButtonsLoading(true, 'generate');
    showStatus('loading', 'جاري توليد معلومات التحليل بالذكاء الاصطناعي...');

    try {
      const payload = {
        name: name,
        price: price ? parseFloat(price) : null,
        duration: durationInput ? durationInput.value.trim() : '',
        sample_type: sampleInput ? sampleInput.value.trim() : '',
        description: document.getElementById('testDescriptionInput')?.value || '',
        patient_instructions: document.getElementById('testInstructionsInput')?.value || '',
      };

      const response = await fetch('/tests/generate', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'حدث خطأ أثناء توليد البيانات.');
      }

      // Populate AI fields directly into the form
      populateFormWithAIData(data);

      showStatus(
        'success',
        isAuto
          ? 'تم اقتراح بيانات التحليل تلقائياً بواسطة الذكاء الاصطناعي.'
          : 'تم توليد البيانات بنجاح. يمكنك التعديل عليها واعتمادها قبل الحفظ.'
      );
    } catch (err) {
      showStatus('error', err.message || 'تعذر الاتصال بالخادم لتوليد البيانات.');
    } finally {
      isGenerating = false;
      setButtonsLoading(false, 'generate');
    }
  }

  // ── Regeneration Function ───────────────────────────────────
  async function triggerRegenerate() {
    if (isGenerating) return;

    const name = nameInput ? nameInput.value.trim() : '';
    if (!name) {
      showStatus('error', 'يرجى إدخال اسم التحليل الطبي أولاً.');
      if (nameInput) nameInput.focus();
      return;
    }

    isGenerating = true;
    setButtonsLoading(true, 'regenerate');
    showStatus('loading', 'جاري إعادة صياغة واقتراح المعلومات بصيغة بديلة...');

    try {
      const currentKeywords = getTagValues('keywordsContainer');
      const currentAliases = getTagValues('aliasesContainer');

      const payload = {
        name: name,
        price: priceInput ? parseFloat(priceInput.value.trim()) || null : null,
        previous_output: {
          description: document.getElementById('testDescriptionInput')?.value || '',
          patient_instructions: document.getElementById('testInstructionsInput')?.value || '',
          duration: durationInput ? durationInput.value.trim() : '',
          sample_type: sampleInput ? sampleInput.value.trim() : '',
          keywords: currentKeywords,
          aliases: currentAliases,
        },
      };

      const response = await fetch('/tests/regenerate', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'حدث خطأ أثناء إعادة التوليد.');
      }

      // Populate with new draft
      populateFormWithAIData(data, true); // Force update fields on manual regeneration
      showStatus('success', 'تمت إعادة صياغة واقتراح البيانات بنجاح.');
    } catch (err) {
      showStatus('error', err.message || 'تعذر الاتصال بالخادم لإعادة التوليد.');
    } finally {
      isGenerating = false;
      setButtonsLoading(false, 'regenerate');
    }
  }

  // ── Populate Form with AI Data ──────────────────────────────
  function populateFormWithAIData(data, forceOverwrite = false) {
    const descInput = document.getElementById('testDescriptionInput');
    const instInput = document.getElementById('testInstructionsInput');

    if (descInput && (forceOverwrite || !descInput.value.trim()) && data.description) {
      descInput.value = data.description;
    }

    const instructions = data.patient_instructions || data.instructions;
    if (instInput && (forceOverwrite || !instInput.value.trim()) && instructions) {
      instInput.value = instructions;
    }

    // Fill duration only if empty, or if forced
    if (durationInput && (forceOverwrite || !durationInput.value.trim()) && data.duration) {
      durationInput.value = data.duration;
    }

    // Fill sample type only if empty, or if forced
    if (sampleInput && (forceOverwrite || !sampleInput.value.trim()) && data.sample_type) {
      sampleInput.value = data.sample_type;
    }

    // Set keywords
    if (data.keywords && Array.isArray(data.keywords)) {
      setTagValues('keywordsContainer', 'keywords', data.keywords);
    }

    // Set aliases
    const aliases = data.aliases || data.alias_name;
    if (aliases && Array.isArray(aliases)) {
      setTagValues('aliasesContainer', 'alias_name', aliases);
    }

    // Subtle visual feedback pulse
    [descInput, instInput, durationInput, sampleInput].forEach(el => {
      if (el) {
        el.classList.remove('ai-field-updated');
        void el.offsetWidth; // reflow
        el.classList.add('ai-field-updated');
      }
    });

    if (window.lucide) lucide.createIcons();
  }

  // ── Status Banner Helper ────────────────────────────────────
  function showStatus(type, message) {
    const box = document.getElementById('aiStatusBox');
    if (!box) return;

    box.className = `ai-status-box ${type}`;
    let iconHtml = '';

    if (type === 'loading') {
      iconHtml = '<span class="spinner"></span>';
    } else if (type === 'success') {
      iconHtml = '<i data-lucide="check-circle" style="width: 16px; height: 16px;"></i>';
    } else if (type === 'error') {
      iconHtml = '<i data-lucide="alert-circle" style="width: 16px; height: 16px;"></i>';
    }

    box.innerHTML = `${iconHtml}<span>${message}</span>`;
    box.style.display = 'flex';

    if (window.lucide) lucide.createIcons();

    if (type === 'success') {
      setTimeout(() => {
        if (box.classList.contains('success')) {
          box.style.display = 'none';
        }
      }, 5000);
    }
  }

  // ── Button Loading States ───────────────────────────────────
  function setButtonsLoading(isLoading, action) {
    if (generateBtn) {
      generateBtn.disabled = isLoading;
      if (isLoading && action === 'generate') {
        generateBtn.innerHTML = '<span class="spinner"></span> جاري التوليد...';
      } else {
        generateBtn.innerHTML = '<i data-lucide="sparkles"></i> توليد بالذكاء الاصطناعي';
      }
    }

    if (regenerateBtn) {
      regenerateBtn.disabled = isLoading;
      if (isLoading && action === 'regenerate') {
        regenerateBtn.innerHTML = '<span class="spinner"></span> جاري إعادة التوليد...';
      } else {
        regenerateBtn.innerHTML = '<i data-lucide="refresh-cw"></i> إعادة التوليد';
      }
    }

    if (window.lucide) lucide.createIcons();
  }
}

// ============================================================
// Tag / Chip Management Functions
// ============================================================

function initTagInput(containerId, textInputId, inputName) {
  const container = document.getElementById(containerId);
  const textInput = document.getElementById(textInputId);
  if (!container || !textInput) return;

  container.addEventListener('click', (e) => {
    if (!e.target.classList.contains('tag-chip-remove')) {
      textInput.focus();
    }
  });

  textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      commitPendingTag(containerId, textInputId, inputName);
    } else if (e.key === 'Backspace' && textInput.value === '') {
      const chips = container.querySelectorAll('.tag-chip');
      if (chips.length > 0) {
        chips[chips.length - 1].remove();
      }
    }
  });

  textInput.addEventListener('blur', () => {
    commitPendingTag(containerId, textInputId, inputName);
  });
}

function commitPendingTag(containerId, textInputId, inputName) {
  const textInput = document.getElementById(textInputId);
  if (!textInput) return;

  const value = textInput.value.replace(/,/g, '').trim();
  if (value) {
    addTag(containerId, inputName, value);
    textInput.value = '';
  }
}

function addTag(containerId, inputName, text) {
  const container = document.getElementById(containerId);
  if (!container || !text) return;

  const existingValues = getTagValues(containerId);
  if (existingValues.includes(text.toLowerCase())) return;

  const chip = document.createElement('span');
  chip.className = 'tag-chip';
  chip.innerHTML = `
    <span>${escapeHtml(text)}</span>
    <input type="hidden" name="${inputName}" value="${escapeHtml(text)}">
    <button type="button" class="tag-chip-remove" title="حذف">&times;</button>
  `;

  chip.querySelector('.tag-chip-remove').addEventListener('click', (e) => {
    e.stopPropagation();
    chip.remove();
  });

  const textInput = container.querySelector('.tag-input-field');
  if (textInput) {
    container.insertBefore(chip, textInput);
  } else {
    container.appendChild(chip);
  }
}

function setTagValues(containerId, inputName, tagsArray) {
  const container = document.getElementById(containerId);
  if (!container) return;

  container.querySelectorAll('.tag-chip').forEach(c => c.remove());

  tagsArray.forEach(t => {
    if (typeof t === 'string' && t.trim()) {
      addTag(containerId, inputName, t.trim());
    }
  });
}

function getTagValues(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return [];
  const inputs = container.querySelectorAll('input[type="hidden"]');
  return Array.from(inputs).map(i => i.value.trim().toLowerCase()).filter(v => v);
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ============================================================
// 3-Step Import Modal & Export / Retry JS Workflow
// ============================================================

function initImportModalWorkflow() {
  const openModalBtn = document.getElementById('openImportModalBtn');
  const modal = document.getElementById('importModal');
  const closeModalBtn = document.getElementById('closeImportModalBtn');
  const exportBtn = document.getElementById('exportPricesBtn');
  const retryBtn = document.getElementById('retryPendingBtn');

  if (exportBtn) {
    exportBtn.addEventListener('click', () => {
      const citySelect = document.getElementById('importCitySelect');
      const urlParams = new URLSearchParams(window.location.search);
      const cityId = citySelect ? citySelect.value : (urlParams.get('city_id') || '');
      if (!cityId || cityId === 'all') {
        alert('يرجى اختيار منطقة محددة أولاً لتصدير أسعارها.');
        return;
      }
      window.location.href = `/prices/export?city_id=${cityId}`;
    });
  }

  if (retryBtn) {
    retryBtn.addEventListener('click', async () => {
      const urlParams = new URLSearchParams(window.location.search);
      const cityId = urlParams.get('city_id');
      if (!cityId || cityId === 'all') {
        alert('يرجى تحديد المنطقة أولاً لإعادة محاولة التحاليل المعلقة بها.');
        return;
      }
      retryBtn.disabled = true;
      retryBtn.innerHTML = '<span class="spinner"></span> جاري البدء...';
      try {
        const resp = await fetch('/prices/retry_pending', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify({ city_id: parseInt(cityId) })
        });
        const data = await resp.json();
        if (resp.ok) {
          alert(`تم جدولتها بنجاح (${data.queued || 0} تحليل معلق سيتم معالجته بالخلفية).`);
          window.location.reload();
        } else {
          alert(data.error || 'فشلت عملية إعادة المحاولة');
        }
      } catch (err) {
        alert('حدث خطأ أثناء التواصل مع الخادم');
      } finally {
        retryBtn.disabled = false;
        retryBtn.innerHTML = '<i data-lucide="refresh-cw"></i> إعادة محاولة المعلّق';
        if (window.lucide) lucide.createIcons();
      }
    });
  }

  if (!modal) return;

  let currentPreviewToken = null;
  let progressInterval = null;

  if (openModalBtn) {
    openModalBtn.addEventListener('click', () => {
      resetImportModal();
      modal.style.display = 'flex';
    });
  }

  if (closeModalBtn) {
    closeModalBtn.addEventListener('click', () => {
      modal.style.display = 'none';
      if (progressInterval) clearInterval(progressInterval);
    });
  }

  document.getElementById('importCloseDoneBtn')?.addEventListener('click', () => {
    modal.style.display = 'none';
    window.location.reload();
  });

  const nextBtn = document.getElementById('importNextBtn');
  const backBtn = document.getElementById('importBackBtn');
  const commitBtn = document.getElementById('importCommitBtn');

  // STEP 1 -> STEP 2 (Preview)
  if (nextBtn) {
    nextBtn.addEventListener('click', async () => {
      const fileInput = document.getElementById('importFileInput');
      const citySelect = document.getElementById('importCitySelect');
      const deleteCheck = document.getElementById('importDeleteMissingCheck');
      const errBox = document.getElementById('importStep1Error');

      errBox.style.display = 'none';
      if (!fileInput.files.length) {
        errBox.innerText = 'يرجى اختيار ملف أسعار أولاً';
        errBox.style.display = 'block';
        return;
      }
      if (!citySelect.value) {
        errBox.innerText = 'يرجى اختيار المنطقة المستهدفة';
        errBox.style.display = 'block';
        return;
      }

      nextBtn.disabled = true;
      nextBtn.innerText = 'جاري معاينة الملف...';

      const formData = new FormData();
      formData.append('file', fileInput.files[0]);
      formData.append('city_id', citySelect.value);
      formData.append('delete_missing', deleteCheck.checked ? 'true' : 'false');

      try {
        const resp = await fetch('/prices/import/preview', {
          method: 'POST',
          body: formData,
        });
        const data = await resp.json();

        if (!resp.ok || data.error) {
          throw new Error(data.error || 'فشلت معاينة الملف');
        }

        currentPreviewToken = data.token;

        // Populate Step 2 counters
        document.getElementById('cntCreated').innerText = data.created || 0;
        document.getElementById('cntUpdated').innerText = data.updated || 0;
        document.getElementById('cntUnchanged').innerText = data.unchanged || 0;
        document.getElementById('cntSkipped').innerText = (data.skipped || 0) + (data.needs_review_count || 0);

        // Delete missing warning box
        const deleteBox = document.getElementById('wouldDeleteBox');
        if (data.would_delete && data.would_delete.count > 0) {
          document.getElementById('wouldDeleteCount').innerText = data.would_delete.count;
          const namesList = (data.would_delete.names || []).join('، ');
          document.getElementById('wouldDeleteNames').innerText = `أمثلة: ${namesList}`;
          deleteBox.style.display = 'block';
        } else {
          deleteBox.style.display = 'none';
        }

        // Needs review list
        const reviewBox = document.getElementById('needsReviewBox');
        const reviewList = document.getElementById('needsReviewList');
        const reviewCnt = document.getElementById('cntNeedsReview');
        if (data.needs_review && data.needs_review.length > 0) {
          reviewCnt.innerText = data.needs_review.length;
          reviewList.innerHTML = data.needs_review.map(item =>
            `<div>• الصف ${item.row}: <strong>${escapeHtml(item.name || 'بدون اسم')}</strong> — ${escapeHtml(item.reason || '')}</div>`
          ).join('');
          reviewBox.style.display = 'block';
        } else {
          reviewBox.style.display = 'none';
        }

        // Switch to Step 2
        setImportStep(2);
      } catch (err) {
        errBox.innerText = err.message || 'حدث خطأ أثناء معاينة الملف';
        errBox.style.display = 'block';
      } finally {
        nextBtn.disabled = false;
        nextBtn.innerText = 'معاينة الملف';
      }
    });
  }

  // Back button (Step 2 -> Step 1)
  if (backBtn) {
    backBtn.addEventListener('click', () => {
      setImportStep(1);
    });
  }

  // STEP 2 -> STEP 3 (Commit)
  if (commitBtn) {
    commitBtn.addEventListener('click', async () => {
      const citySelect = document.getElementById('importCitySelect');
      const deleteCheck = document.getElementById('importDeleteMissingCheck');
      const aiCheck = document.getElementById('importGenerateAiCheck');
      const errBox = document.getElementById('importStep2Error');

      errBox.style.display = 'none';
      commitBtn.disabled = true;
      commitBtn.innerText = 'جاري البدء...';

      try {
        const payload = {
          token: currentPreviewToken,
          city_id: parseInt(citySelect.value),
          delete_missing: deleteCheck.checked,
          generate_ai: aiCheck.checked,
        };

        const resp = await fetch('/prices/import/commit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await resp.json();

        if (!resp.ok || data.error) {
          throw new Error(data.error || 'فشلت عملية الحفظ والتأكيد');
        }

        // Switch to Step 3 and poll progress
        setImportStep(3);
        startProgressPolling(currentPreviewToken);

      } catch (err) {
        errBox.innerText = err.message || 'حدث خطأ أثناء تنفيذ الحفظ';
        errBox.style.display = 'block';
      } finally {
        commitBtn.disabled = false;
        commitBtn.innerText = 'تأكيد وتنفيذ الاستيراد';
      }
    });
  }

  function setImportStep(stepNum) {
    document.getElementById('importStep1').style.display = stepNum === 1 ? 'block' : 'none';
    document.getElementById('importStep2').style.display = stepNum === 2 ? 'block' : 'none';
    document.getElementById('importStep3').style.display = stepNum === 3 ? 'block' : 'none';

    document.getElementById('stepIndicator1').className = `import-step ${stepNum >= 1 ? 'active' : ''}`;
    document.getElementById('stepIndicator2').className = `import-step ${stepNum >= 2 ? 'active' : ''}`;
    document.getElementById('stepIndicator3').className = `import-step ${stepNum >= 3 ? 'active' : ''}`;

    nextBtn.style.display = stepNum === 1 ? 'inline-block' : 'none';
    backBtn.style.display = stepNum === 2 ? 'inline-block' : 'none';
    commitBtn.style.display = stepNum === 2 ? 'inline-block' : 'none';
    document.getElementById('importCloseDoneBtn').style.display = stepNum === 3 ? 'inline-block' : 'none';
  }

  function resetImportModal() {
    currentPreviewToken = null;
    if (progressInterval) clearInterval(progressInterval);
    document.getElementById('importPreviewForm').reset();
    document.getElementById('importStep1Error').style.display = 'none';
    document.getElementById('importStep2Error').style.display = 'none';
    document.getElementById('importFinalAlert').style.display = 'none';
    setImportStep(1);
  }

  function startProgressPolling(token) {
    if (progressInterval) clearInterval(progressInterval);

    progressInterval = setInterval(async () => {
      try {
        const resp = await fetch(`/prices/import/progress/${token}`);
        if (!resp.ok) return;

        const data = await resp.json();
        const total = data.total || 0;
        const done = data.done || 0;
        const failed = data.failed || 0;
        const pending = data.pending || 0;
        const pct = total > 0 ? Math.round(((done + failed) / total) * 100) : 100;

        document.getElementById('importProgressBar').style.width = `${pct}%`;
        document.getElementById('importProgressText').innerText = `جاري المعالجة... ${pct}%`;
        document.getElementById('statDone').innerText = done;
        document.getElementById('statFailed').innerText = failed;
        document.getElementById('statPending').innerText = pending;
        document.getElementById('statTotal').innerText = total;

        if (data.status === 'completed' || (total > 0 && done + failed >= total)) {
          clearInterval(progressInterval);
          document.getElementById('importProgressText').innerText = 'اكتملت المعالجة 100%';
          document.getElementById('importFinalAlert').style.display = 'block';
        }
      } catch (e) {
        console.error('Progress polling error:', e);
      }
    }, 1000);
  }
}

window.toggleReviewList = function() {
  const list = document.getElementById('needsReviewList');
  const chev = document.getElementById('reviewChevron');
  if (!list) return;
  if (list.style.display === 'none') {
    list.style.display = 'block';
    if (chev) chev.style.transform = 'rotate(180deg)';
  } else {
    list.style.display = 'none';
    if (chev) chev.style.transform = 'rotate(0deg)';
  }
};