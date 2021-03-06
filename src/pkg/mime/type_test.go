// Copyright 2010 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

package mime

import "testing"

var typeTests = map[string]string{
	".t1":  "application/test",
	".t2":  "text/test; charset=utf-8",
	".png": "image/png",
}

func TestTypeByExtension(t *testing.T) {
	typeFiles = []string{"test.types"}

	for ext, want := range typeTests {
		val := TypeByExtension(ext)
		if val != want {
			t.Errorf("TypeByExtension(%q) = %q, want %q", ext, val, want)
		}

	}
}

func TestCustomExtension(t *testing.T) {
	custom := "text/xml; charset=iso-8859-1"
	if error := AddExtensionType(".xml", custom); error != nil {
		t.Fatalf("error %s for AddExtension(%s)", error, custom)
	}
	if registered := TypeByExtension(".xml"); registered != custom {
		t.Fatalf("registered %s instead of %s", registered, custom)
	}
}
